# Module Spec — Endpoint health, alerting & auto-remediation

> Phase 16 (`docs/06-roadmap.md`), expanded from the scoping brief
> `docs/modules/endpoint-health-alerting.md`.
>
> One new agent capability (`health.check`) and one new agent→server wire
> message. Remediation adds **no** new action code: it dispatches existing,
> registered job types through the unmodified signed-job engine.
>
> **Hard dependencies:** Inventory (Phase 1, for the device fleet), notification
> core (Service Desk sub-project 2 — the outbox and delivery), and, for the
> `open_ticket` action only, the ticket core (Service Desk sub-project 1). See
> §1.3 for what must be deferred if this module is pulled forward ahead of
> Service Desk, which `docs/06-roadmap.md`'s Phase 16 note recommends.

---

## 0. Resolved design decisions

Twelve decisions settled before the design, because each rules out a family of
alternatives. Every one names what it costs, including the perverse incentives
it accepts — a monitoring/alerting module fails quietly, by alarming on the
wrong things or not clearing, and the failure modes are all in these choices.

### 0.1 A check is a builtin type, never a free-form expression

A health check is a row drawn from a **closed registry of builtin check types**
(§4.1), parameterised by `(selector, comparator, threshold, for_ticks,
severity)`. There is no expression language, no query DSL, and no
"run this script and alarm on its exit code".

Rejected: **a free-form expression evaluated on the agent**. Two independent
reasons, and the second is the load-bearing one:

- The check policy travels as a signed job (§0.4), so an expression would at
  least be *authorised*. But an evaluator is an interpreter with an unbounded
  cost profile, and the idle claim in §0.5 is only checkable because the work
  per tick is a fixed, enumerable set of counter reads. An expression language
  makes the idle budget a statement about what customers happen to have
  written, which is not a budget.
- A closed set is what lets the server know, per OS, whether a check is even
  *answerable* — which is what §0.7's `unavailable` state depends on.

Also rejected: **script-as-check** (`script.run` on a timer, alarm on exit
code). That is an always-on process-spawning loop on every endpoint, and it is
the single fastest way to convert this module into the thing
`docs/05-agent-spec.md` forbids.

**The cost, stated:** a customer whose check we do not ship has no escape hatch,
and the pressure will be to add exactly the script-check above. Adding a check
type is a code change on **both sides** — the control plane and the Go agent
under `agent/` — plus an edit to §4.1 — deliberately not a configuration row, so
the decision is reviewed rather than typed into a form at 2am. Since the agent
moved in-tree (`CLAUDE.md`, `a32cead`) those two changes fit in one commit,
which removes the coordination cost and **not** the compatibility one: a policy
push carrying a check type an older deployed agent has never compiled in still
reaches devices that were enrolled before the change. §4.1's closed registry is
a *server-side* set, so what an old agent does with an unrecognised
`check_type` is an agent-side behaviour that is not yet specified and is not
tested anywhere — it must be settled when the capability is written, not
assumed. The fleet is always mixed-version; one repo does not make it uniform. When the real requirement is
"alarm on a trend", that is the monitoring product (§0.12), and the answer is a
referral, not a new check type.

### 0.2 Checks live in profiles; a device gets exactly one effective profile

`health_profile` holds an ordered set of `health_check` rows. A device's
effective profile is resolved from `health_profile_assignment` — most specific
first: **device → nearest assigned group ancestor → org default**. First match
wins; **exactly one profile is ever effective for a device**.

This is the `sla_policy` resolution shape (`docs/modules/sla/spec.md` §3),
reused deliberately, including its enforcement mechanism: **partial unique
indexes per resolution level**, because NULLs do not collide in a plain
Postgres unique constraint and "two device-level assignments for one device"
would otherwise be representable and resolved by a tiebreak nobody chose.

```sql
CREATE UNIQUE INDEX uq_health_assignment_device ON health_profile_assignment
  (org_id, device_id)       WHERE device_id IS NOT NULL;
CREATE UNIQUE INDEX uq_health_assignment_group  ON health_profile_assignment
  (org_id, device_group_id) WHERE device_group_id IS NOT NULL;
CREATE UNIQUE INDEX uq_health_assignment_client_org ON health_profile_assignment
  (org_id, client_id) NULLS NOT DISTINCT
                            WHERE device_id IS NULL AND device_group_id IS NULL;
```

plus `CHECK (NOT (device_id IS NOT NULL AND device_group_id IS NOT NULL))`.

The org-default index was `uq_health_assignment_org (org_id)` until MSP
clients migration 0231 (Task 8b) made it per client: the org's default is the
row with no client, unique among those rows, and a client may hold a default of
its own beside it. A device's and a group's assignment already belong to their
parent's client.

Rejected: **union of every matching profile** (the Atera-ish "all applicable
profiles apply"). Union has no way to express *removal*: a check an operator
silenced for one troublesome device is resurrected by the org-wide profile, and
the only fix is an anti-check, i.e. a second config concept. Resolution by
specificity means "this device is different" is expressible in one row.

**The cost, stated:** an operator who wants the org profile *plus* one extra
check on a device must clone the org profile and add to it, and the clone then
drifts from its source. Mitigations, both required in v1: profile **clone** is a
first-class API action (§10), and the device detail view shows the effective
profile **and which assignment produced it** — a resolution nobody can see is a
resolution nobody trusts.

The group walk follows `device_groups.parent_group_id` upward. It is
**depth-bounded (16)**: that column has no cycle constraint in the kernel
schema, and an unbounded walk over a cycle is a hung worker. Exceeding the
bound degrades to "no group match" (falling through to the org default) and
logs at ERROR with the device id — the same degrade-don't-fail posture
`docs/modules/sla/spec.md` §3.1 takes for a broken calendar.

### 0.3 Duration is expressed in consecutive ticks, not wall-clock

A check breaches after `for_ticks` **consecutive** matching samples (default 3,
CHECK `BETWEEN 1 AND 60`). It is not expressed in seconds.

A wall-clock duration is a lie the moment two devices run different tick
intervals: "CPU > 95% for 10 minutes" is 2 samples at a 300 s tick and 10 at a
60 s tick, and the two behave completely differently on a spiky workload. Ticks
are the unit the agent actually counts.

**The cost, stated:** operators think in minutes. Mitigation: the API accepts
`for_seconds` as an alternative input, converts with `ceil(for_seconds /
tick_interval_s)` against the *profile's* tick interval, stores the tick count,
and returns both — and the UI renders "3 ticks (≈15 min at this profile's
5-minute tick)". Changing a profile's tick interval therefore visibly changes
every check's effective duration, which is true, and is better surfaced than
hidden behind a stored seconds value that silently means something new.

### 0.4 The agent owns the tick; the policy reaches it as a signed job

Rejected: **the server dispatches a `health.check` job per device per tick.**
At a 5-minute tick and 5,000 devices that is 60,000 signed jobs an hour and
~1.4M `jobs` rows a day. The job table becomes a metric stream, `SigningService`
becomes a hot path, and the audit log (which records `jobs.dispatched` for every
one of them — `app/jobs/engine.py`) becomes unreadable. The idea does not
survive its own arithmetic.

Chosen: **the agent's existing local scheduler owns the tick**
(`docs/05-agent-spec.md`, "Local scheduler" — timer-driven, no polling).

That raises the real question: how does a *threshold configuration* reach an
agent without inventing an unsigned server→agent control message? Verified
against the code: `app/gateway/protocol.py` defines exactly one server→agent
message, `JobEnvelope`. There is no policy channel, and inventing one would be
a new instruction path into a root/SYSTEM process that the signed-job runner
does not verify — invariant #1, straightforwardly.

Therefore **the policy is delivered as a signed job**: new job type
`health.policy.apply` (§5.2), whose params are the device's resolved check set,
tick interval, and `policy_version`. The agent verifies signature, target
identity, freshness and non-replay exactly as for any other job, *then*
installs the policy. Threshold configuration inherits the entire signed-job
safety story for free, and no new server→agent message kind exists.

Dispatched on: first connect after enrollment; any change to the device's
effective profile, its checks, or its assignment; and a slow reconciliation
sweep (§12) that re-pushes to any device whose last acknowledged
`policy_version` is behind. The agent reports its installed `policy_version` in
every summary (§5.1), which is what makes the reconciliation sweep a comparison
rather than a broadcast.

> **Doc/code divergence, recorded:** `docs/05-agent-spec.md` §"Enrollment &
> identity" step 4 says the agent "receives its resolved **policy**" at
> enrollment. `EnrollResponse` (`backend/app/api/v1/enrollment.py`) carries
> `device_id`, `cert_pem`, `chain_pem`, `ca_bundle_pem`, `gateway_url`,
> `job_signing_public_key_pem` — and no policy. This spec does not add one; it
> uses the signed-job path above, which is strictly safer than an
> enrollment-response policy blob would have been.

### 0.5 What the agent holds, and the idle claim this module is accountable for

**Evaluated at the edge:** every per-tick sample, its comparator, and its
consecutive-tick counter. **Evaluated on the server:** `agent.offline` only
(§0.6), plus the entire alert lifecycle, dedup, flap suppression and actions.

Per check, the agent holds a fixed-size struct — `consecutive int32`,
`state uint8`, `last float32` — and **no sample history at all**. There is no
ring buffer, no rolling average, no "last 10 values". A moving average is a
time series, and a time series on the endpoint is the first step to becoming
the monitoring product (§0.12).

**The claim.** With the default profile (≤ 12 checks, 300 s fast tick, 3600 s
posture sub-tick), `health.check` adds **< 2 MB resident memory** and **< 0.1%
of one core averaged over the tick interval**, and performs **zero network
writes on a tick where nothing changed**.

**Mechanism** (this is what makes the number achievable, not aspirational): one
entry on the existing timer wheel — no new goroutine parked in a loop; per-check
state is ~16 bytes × checks; sample buffers are reused, never appended to; the
expensive posture probes are on the slow sub-tick, not the fast tick.

**What falsifies it.** Any of the following is a **defect**, not a tuning
matter (`CLAUDE.md`: "Regression = defect"):

1. RSS delta above 2 MB on the default profile.
2. Any per-tick heap allocation proportional to elapsed ticks (i.e. anything
   that accumulates).
3. Posture probes (BitLocker/FileVault/LUKS state, AV health, firewall state —
   the WMI / `powershell` / `sc` / `systemctl` shell-outs) running on the fast
   tick instead of the posture sub-tick.
4. A network write on a tick with no state transition and no summary due.
5. An event-log check type opening its **own** OS log subscription (see below).
6. A profile permitted to exceed the caps in §0.11.

**Amendment — where these falsifiers are checked, stated plainly.** *Nowhere in
this repository.* That much is still true. The reason given below it was not, and
is corrected in the third amendment.

*Superseded, and left standing so the correction is legible:* "Verified against
`.github/workflows/ci.yml`, which runs exactly three jobs: `backend`
(`ruff check`, `mypy app`, `pytest -q`), `deploy` (compose first boot plus the
`app_user` RLS-privilege assertions), and `frontend` (`npm test`,
`npm run typecheck`, `npm run build`). There is no Go toolchain and no memory or
allocation benchmark of any kind in this repo."

**Second amendment, 2026-08-05 — the excuse expired; the six falsifiers did
not.** The paragraph that stood here attributed the gap to the agent living in a
separate `it-suite-agent` repository, "and only that repo's CI can assert them".
`CLAUDE.md` commit `a32cead` moved the agent in-tree to `agent/`. Restating the
position honestly:

- **All six falsifiers are still unrun.** Not one of them has ever been
  measured, by this CI or any other. Nothing about them was satisfied by the
  move.
- **The `health.check` capability is still unwritten.** It has no
  implementation anywhere; there is not yet an `agent/` directory. The work is
  no longer *blocked on another repository* — it is simply *not done*, and it
  is now doable here.
- **There is no longer a reason for the gap, only a cause.** "The code is where
  CI cannot reach it" was a true explanation until the move and is now false.
  The benchmark is missing because nobody wrote it. `CLAUDE.md`'s idle-budget
  section now requires a benchmark that runs plus a stated falsifier before any
  footprint number may be reported as verified, and a Go CI job to run it;
  neither exists yet.

**Third amendment, 2026-08-12 — the Go CI job now exists and runs every step;
the six falsifiers are still unrun.** *(Heading corrected 2026-08-13: it read
"exists and is green". Nobody here has observed a green run in Actions — see
`HANDOFF.md` §6, which says so — and the packaging `sudo`/`stat` blocker was
found after this was written, so it was not green. What is verifiable is that
the job's steps run and pass locally under the workflow's own commands; whether
the latest Actions run is green has to be looked at, not asserted.)* Two
statements above have expired and are corrected here rather than edited away:

- "There is no Go toolchain and no memory or allocation benchmark of any kind
  in this repo" is **false**. `.github/workflows/ci.yml` has a fourth job,
  `agent`, pinning Go 1.24 and running `gofmt`, `go vet`, `staticcheck`,
  `go test ./...`, six cross-compile targets, the packaging build, and an
  idle-budget step that prints the measured numbers into the build log. Other
  capabilities — `backup` among them — already have allocation and RSS
  falsifiers running there.
- "There is not yet an `agent/` directory" is **false**. It exists and carries
  the transport, the signed-job engine, the dispatcher and ten capability
  packages.

That job was RED until 2026-08-12, which is its own admission: a job that
cannot finish asserts nothing, so for a period the toolchain existed and still
checked nothing. The blockers were a `GOOS=windows go vet` compile failure in
an untagged test and ten `staticcheck` findings; both are cleared.

**Fourth amendment, same day, correcting the third.** The paragraph that stood
here said "all six falsifiers in §0.5 remain unrun, because `health.check`
remains unwritten", and the numbers were "a design target". **Both are false,
and the error was written while correcting other stale claims — the failure
mode this document keeps recording, committed by the person recording it.**

What is actually in the tree, verified by running it rather than by reading
§6 of `HANDOFF.md` or `docs/06-roadmap.md`:

- **The capability is implemented and wired.** `agent/internal/capabilities/health/`
  holds the tick engine, the closed check-type registry with all fourteen types
  of §4.1, policy application, and the report path. `main.go` binds it and
  registers both job types, `health.policy.apply` and `health.check.now`.
- **All six falsifiers exist as ordinary Go tests and pass.**
  `TestFalsifier1_RSSDeltaOnTheDefaultProfileStaysUnder2MB`,
  three `TestFalsifier2_*`, `TestFalsifier3_PostureProbesNeverRunOnTheFastTick`,
  `TestFalsifier4_AQuietTickWritesNothingToTheGateway`,
  `TestFalsifier5_NoEventlogCheckOpensItsOwnLogSubscription` and
  `TestFalsifier6_APolicyOverTheCapsNeverArmsTheTickEngine` — eight tests for
  six falsifiers. They are matched by the `Falsifier` alternative in the CI
  idle-budget step's `-run` pattern, so they execute on every push now that the
  `agent` job is unblocked.

**What IS still missing, stated precisely so the next reader does not have to
re-derive it:**

- *Superseded 2026-08-14, see the seventh amendment below:* "**Fast-tick
  sampling on Windows and macOS.** `sampler_other.go` answers `unavailable`
  for every check. Deliberate and argued in place: both platforms need real
  counters rather than a fast-tick shell-out, which is the shape falsifier 3
  forbids." False of Windows as of the seventh amendment
  (`sampler_windows.go`), and, as of 2026-08-14, only partly true of macOS:
  `sampler_darwin.go` answers `disk.free_pct`, `disk.free_bytes` and
  `process.present`. It leaves `cpu.busy_pct` and `memory.used_pct`
  `unavailable` — still for the falsifier-3 reason, now sharpened: Mach is
  unreachable without cgo, and a hand-rolled binding cannot be verified by any
  check this project runs. See the §4.1 macOS amendment.
- *Superseded 2026-08-13, see the fifth amendment below:* "Every posture
  probe, on every platform, and `disk.smart_ok` with them. `posture.go`
  answers `unavailable`. Also deliberate: each probe is a footprint argument
  of its own and must land on the posture sampler so falsifier 3 keeps
  measuring something." That was never literally about `posture.go` — that
  file is the cross-platform seam (deadline, overlap guard, runner), not a
  per-OS probe table — and the per-OS files it meant are no longer accurate
  either.

`unavailable` is a first-class state (§0.7) — stored, surfaced, countable, and
it never opens or clears an alert — so a Windows fleet on this build is not
silently reported healthy.

**Fifth amendment, 2026-08-13 — the posture probes now exist; the fourth
amendment's "still missing" list was wrong on that point by the time it was
written.** What is actually in the tree, verified by running it:

- **Linux is implemented and its dispatch has actually executed.**
  `posture.disk_encryption`, `posture.firewall_enabled` and `disk.smart_ok`
  are implemented in `posture_linux_probes.go` and wired in `posture_linux.go`.
  The decision logic is unit-tested through an injected runner
  (`posture_test.go`), and `posture_linux_integration_test.go` additionally
  runs the REAL `findmnt`/`lsblk`/`nft`/`iptables` on the machine running the
  test — observed reporting `encrypted=false (device /dev/vda)` and a
  definite firewall answer — skipping loudly, not silently, when a tool is
  absent. Five posture-specific idle-budget falsifiers also pass
  (`posture_budget_test.go`, Falsifiers A–E, distinct from the six in §0.5
  because these probes spawn processes and the original six do not): a pass
  retains nothing across a hundred repeats; a pass leaves no goroutine behind
  across a real spawn per iteration (`testing.Benchmark`-chosen iteration
  count, so the total spawn count is runner-dependent — 1117 on one observed
  run); a hung command cannot hold the pass open (a 60s
  command under a 5s deadline returns in ~7s); a second concurrent pass runs
  zero commands of its own; and a pass of wedged checks is bounded by
  `posturePassBudget`, answering `unavailable` and never a verdict for the
  checks it then skips. *(Amended 2026-08-13: there were four, and two of them
  could not fail — B drove a fake runner that spawns nothing, and C's
  child-process count was structurally zero. E did not exist, so the design's
  own "a probe cannot hang the tick" was uncovered while a posture pass could
  in fact stall the agent's whole scheduler.)*
- **macOS is implemented but has never run.** `posture.disk_encryption` and
  `posture.firewall_enabled` are implemented in `posture_darwin_probes.go` and
  wired in `posture_darwin.go`. Only the LOGIC is exercised, through the same
  injected-runner seam, on Linux CI — no macOS machine exists in this
  project, so neither the real dispatch nor the real command output formats
  have ever executed. Verified by `GOOS=darwin go vet ./...` only — which
  type-checks the darwin files including their tests, and proves nothing more.
  *(Corrected 2026-08-13: this claimed a `GOOS=darwin go test -c` step too, and
  in fact `ci.yml` had NO `GOOS=darwin` step at all — darwin got only the
  `go build ./cmd/agent` cross-compile, which skips `_test.go` files. The vet
  step was added to the `agent` job by that review.)*
- **Windows is implemented but has never run.** `posture.disk_encryption`,
  `posture.antivirus_healthy`, `posture.firewall_enabled` and `disk.smart_ok`
  are all four implemented in `posture_windows.go`, in-process, through COM —
  no shell-out, by construction (see that file's own header). It is verified
  by `GOOS=windows go vet ./...` in CI, plus a local
  `GOOS=windows go test -c ./internal/capabilities/health/` — CI's
  `go test -c` step covers `./internal/service` only. No Windows machine exists
  in this project, that file has never executed, and its header names **eleven**
  specific things this compile-only verification could not confirm. *(Both
  numbers corrected 2026-08-13: the header lists eleven items, not thirteen,
  and the CI claim was wider than the workflow.)*
- **Still `unavailable`, deliberately, exactly per §4.1's table:**
  `posture.antivirus_healthy` on Linux and macOS (§4.1: Windows-only —
  "AV health" elsewhere has no vendor-neutral answer and inventing one
  produces a check that is confidently wrong) and `disk.smart_ok` on macOS
  (`—` in §4.1). An absent entry in a platform's probe table reports
  `unavailable` (§0.7): stored, surfaced, countable, and it never opens or
  clears an alert.
- **"Implemented" and "verified" are deliberately not used as synonyms
  here.** Linux is both implemented and verified by running its real
  dispatch against real tools. macOS and Windows are implemented and verified
  only to the extent that `go vet`/`go test -c` on the target `GOOS` can
  verify anything without the hardware to run on — which is compilation and
  type-correctness, not behaviour.
- *Superseded 2026-08-14, see the seventh amendment below:* "**Not touched by
  this amendment:** fast-tick sampling on Windows and macOS stays
  `unavailable`, unchanged from the bullet above — this amendment is about the
  POSTURE sampler only, not the fast-tick one, and fast-tick sampling on those
  two platforms is not in this cycle." True when written, about the fifth
  amendment specifically: it touched posture only. No longer true as a
  present-tense description of the fast-tick sampler — a later cycle reached
  Windows fast-tick sampling, and a further one (2026-08-14) reached macOS for
  three of the five checks. Neither platform is "unchanged" any more. See the
  seventh amendment below and the §4.1 macOS amendment.

**Sixth amendment, 2026-08-13 — the two paragraphs that stood here contradicted
the fourth amendment above, in the same section, and were never reconciled
with it.** *Superseded, quoted rather than deleted so the correction is
legible:*

> Until that benchmark exists and is green, §0.5's numbers are a **design
> target, not a measured result**, and §14's "Idle budget" line names an
> obligation rather than describing coverage. This is recorded here rather
> than left implicit because a performance claim that reads as verified, and
> is not, is worse than no claim: it is the sentence a future reviewer will
> cite when approving the change that breaks it.
>
> The cost, stated: this module can ship, pass every gate this repo has, and
> regress the idle budget without anything going red. The only mitigation
> available inside this repo is the one taken in §0.11 — the caps are
> database constraints, so the *configuration* half of the budget is
> enforced here even though the *implementation* half is not.

`git log -S` on this file shows that paragraph pair was written by commit
`294a2ec` ("docs: propagate the agent's move in-tree, without upgrading
anything's status") — before the third and fourth amendments above
established that the Go CI job exists and that the six falsifiers are written,
run, and pass. It was never updated when those amendments landed, so it sat
here asserting the pre-amendment state directly beneath the paragraph that
corrected it.

Checked against the code rather than against either paragraph:
`agent/internal/capabilities/health/budget_test.go` holds
`TestFalsifier1_RSSDeltaOnTheDefaultProfileStaysUnder2MB`,
`TestFalsifier2_TheQuietTickAllocatesNothingAndNothingAccumulates`,
`TestFalsifier2_TheRealHostSamplerAccumulatesNothingEither`,
`TestFalsifier3_PostureProbesNeverRunOnTheFastTick`,
`TestFalsifier4_AQuietTickWritesNothingToTheGateway`,
`TestFalsifier5_NoEventlogCheckOpensItsOwnLogSubscription` and
`TestFalsifier6_APolicyOverTheCapsNeverArmsTheTickEngine`;
`budget_linux_test.go` holds an eighth,
`TestFalsifier2_TheFastTickReadPathDoesNotAllocatePerByteOfTheFileItReads` —
the same eight-tests-for-six-falsifiers count the fourth amendment states.
Run directly (`go test ./internal/capabilities/health/ -run Falsifier -v
-count=1`): all eight pass. §0.5's numbers **are** a measured result for the
six falsifiers stated there, not a design target, and the "design
target"/"pass every gate ... without anything going red" framing above is
false as of the fourth amendment and remained false, unedited, through the
fifth. §14's "Idle budget" line has not been re-checked as part of this
amendment and may still read as it did when that paragraph was written; it is
not corrected here because it is outside the two paragraphs this amendment
addresses.

Item 5 is a real coupling and is stated here rather than discovered later: the
`eventlog.*` check types (§4.1) **reuse the security-logs capability's existing
OS event subscription** — `docs/modules/security-logs/spec.md` §1 already has
an event-driven, always-on subscription on Windows/Linux/macOS, and this module
rides it. If the security-logs capability is not enabled for a device, every
`eventlog.*` check reports `unavailable` (§0.7). It does **not** open a second
subscription to make itself work. Two subscriptions to the same log for two
modules is exactly the "always-on collector" the brief forbids.

**Seventh amendment, 2026-08-14 — Windows fast-tick sampling now exists; it
has never run.** What is actually in the tree, verified by reading the code
rather than by running it — no machine in this project can run Windows code:

- `agent/internal/capabilities/health/sampler_windows.go` implements
  `cpu.busy_pct`, `memory.used_pct`, `disk.free_pct`, `disk.free_bytes` and
  `process.present`. `cpu.busy_pct` comes from `GetSystemTimes` (Windows'
  kernel time already includes idle time, so busy = kernel+user−idle — computed
  in the untagged `sampler_calc.go`, which IS unit-tested, on Linux, since it
  has no `//go:build windows` tag of its own); `memory.used_pct` from
  `GlobalMemoryStatusEx`; `disk.free_pct`/`disk.free_bytes` from
  `GetDiskFreeSpaceEx`, with a `DRIVE_FIXED` filter over
  `GetLogicalDriveStrings` for the `*` wildcard case so a mounted ISO, network
  share or USB stick cannot open a disk-free alert; `process.present` from
  Toolhelp's `PROCESSENTRY32.szExeFile`, matched against an executable name
  only and never a command line (§5.1). Only `GetSystemTimes` and
  `GlobalMemoryStatusEx` are hand-rolled `LazyProc`s; everything else comes
  from `golang.org/x/sys/windows`. No cgo, no WMI, no performance counters, no
  shell-out.
- `service.state` is **not** among them and stays `unavailable` on Windows,
  same as on every other OS (§4.1) — it needs a service-manager integration
  this cycle did not build.
- **This is written and compile-checked, and it has never executed, anywhere.**
  `GOOS=windows go vet ./...` and `GOOS=windows go test -c -o /dev/null
  ./internal/capabilities/health` are its only checks
  (`.github/workflows/ci.yml`, the step renamed accordingly this cycle). No
  Windows machine exists in this project. `sampler_windows_test.go`'s own
  header says so directly: "THESE TESTS COMPILE AND HAVE NEVER RUN." None of the
  five numbers this sampler produces — a CPU percentage, a memory percentage,
  two disk-free readings, a process-present boolean — has ever been compared
  against Task Manager or `wmic`, on any machine.
- **macOS fast-tick sampling is unaffected** *by this (the seventh) amendment*.
  *(Superseded 2026-08-14: it said `sampler_other.go` still answers
  `unavailable` for all five checks. A later cycle changed that —
  `sampler_darwin.go` answers three of the five, and `sampler_other.go` no
  longer compiles for darwin at all. The two Mach-backed checks remain
  `unavailable` as a recorded refusal; see the §4.1 macOS amendment.)*
- **"Implemented" and "verified" are not synonyms here either**, for the same
  reason the fifth amendment states it for the posture probes: this sampler is
  implemented, and verified only to the extent `go vet`/`go test -c` under
  `GOOS=windows` can verify anything without the hardware to run on —
  compilation and type-correctness, not behaviour.

### 0.6 `agent.offline` is a server-side check, and it is the only one

An agent that is offline cannot report that it is offline. Evaluating device
reachability at the edge is a contradiction, so `agent.offline` is evaluated by
a server sweep (§12) against `agents.last_seen_at` and `agents.status`, which
the gateway already maintains.

It is the **only** server-evaluated check, and the check registry marks it
`evaluated_by = 'server'` so the distinction is data, not a special case buried
in a sweep. `health.policy.apply` never ships it to an agent.

### 0.7 Absent is not the same as healthy, and it is not the same as breached

Every check reports one of exactly three states: `ok`, `breaching`,
`unavailable`. A check that cannot be answered on this device — BitLocker on
Linux, SMART on a VM's virtual disk, a named service that is not installed, an
`eventlog.*` check with no security-logs subscription — reports `unavailable`.

`unavailable` **never opens an alert and never clears one**. A "disk encryption
is off" alert fired because the check could not run is worse than no check: it
trains operators to ignore posture alerts. And an alert cleared because the
check stopped being answerable is a silent loss of a real finding.

`unavailable` is nevertheless *visible*: it is a first-class state in
`device_health_check_state`, surfaced in the console, and countable — "37
devices cannot answer the AV check" is an operational fact somebody needs.

§0.8 clause 3 is the one place `unavailable` is allowed to end an alert, and it
is time-boxed and separately attributed for exactly that reason.

### 0.8 The exact predicates that open and clear an alert

The two failure modes this module is designed against are (a) an alert that can
be opened but never cleared and (b) an alert that re-opens every tick. Both are
closed structurally, not by care.

**Dedup key:** `(org_id, device_id, check_id, selector)`.

**The structural guarantee against re-opening:**

```sql
CREATE UNIQUE INDEX uq_health_alert_open_key ON health_alert
  (org_id, device_id, check_id, selector) WHERE state <> 'cleared';
```

At most one non-cleared alert per key, enforced by Postgres. A duplicate breach
report cannot create a second alert; it can only bump `last_seen_at` and
`occurrence_count`.

**OPEN predicate — an inbound `breach` report opens an alert iff ALL of:**

1. the reported `(check_id, selector)` is in the device's **currently effective**
   profile. A breach for a check that has since been removed is discarded and
   **counted**, not silently dropped — see the amendment below for where that
   count goes;
2. the report's `policy_version` is ≥ the device's last *acknowledged* version
   minus one (one version of tolerance, so a breach in flight across a policy
   push is not lost). "Last acknowledged version" is
   `device_health_policy_ack.policy_version` (§2.11); a device with no ack row
   reads as version 0, which makes this clause vacuously true — deliberately, so
   a device that has never acknowledged a policy does not have its breaches
   discarded;
3. no non-cleared alert exists for the key — enforced by the index above, and
   checked in code so the ordinary path is an update rather than a caught
   `IntegrityError`;
4. the key is not inside a flap cooldown (§0.9), *for the purpose of firing
   actions*; the alert row itself is still written (see §0.9).

**Amendment — the discard count has no table, and §0.12's three-table promise
stands.** An earlier draft of clause 1 named `health_ingest_stats`. **No such
table exists and none will be added.** §0.12's "exactly three tables retain
anything" is the monitoring-boundary guarantee this whole module is built to
keep, and it is **reaffirmed unchanged**: `health_alert`,
`device_health_check_state`, `health_flap_state`. A per-batch counter table is a
time series wearing an ops-metrics hat, and it would be the fourth table that
makes the boundary a matter of judgement rather than a property.

Chosen instead — three destinations, none of them durable in this module's
schema:

1. `ingest_report()` **returns** the counts (`applied`, `discarded`) to its
   caller in `app/gateway/dispatch.py`;
2. they ride the post-commit UI event, on exactly the shape
   `_handle_security_events` already publishes (verified —
   `publish_ui_event(redis, org_id, {"kind": ..., "device_id": ..., "count":
   inserted})`): `{"kind": "health_reports_ingested", "device_id": ...,
   "applied": n, "discarded": m}`;
3. a non-zero `discarded` logs at **WARNING** with the device id and the
   offending `(check_id, selector)` pairs, capped at the first 10 per batch so a
   misconfigured fleet cannot turn the log into the metric store the table was
   rejected for being.

Rejected: **a `health_ingest_stats` table**. Also rejected: **an audit record per
discarded line** — §11.3 already excludes per-report ingest from the audit log
for volume, and a hash-chained log a human cannot read is not an audit log.

**The cost, stated:** the count is observable only in the moment. "This device
has been reporting stale check ids for a week" is a question this module's schema
cannot answer, and a discard storm that happens overnight is visible only in
whatever retains the application log. Accepted, because the *cause* is durable
even though the symptom is not: a device reporting stale check ids is a device
whose `device_health_policy_ack.policy_version` (§2.11) is behind its profile's,
which §12's reconciliation sweep already acts on and §13 already renders. Chasing
the cause beats counting the symptom.

**CLEAR predicate — an alert moves to `cleared` iff ANY of:**

1. a `recovery` report arrives for its key — `cleared_reason='recovery'`;
2. a `summary` reports the key as `ok` — `cleared_reason='summary_ok'`. This is
   the repair path for a lost recovery edge (a device that rebooted between
   breach and recovery, an at-least-once buffer that dropped the recovery
   message). **Without this clause every dropped recovery is a permanent
   alert**, which is failure mode (a);
3. a `summary` reports the key as `unavailable` **and** the alert has not been
   reconfirmed as breaching for longer than
   `health_org_settings.unavailable_clear_after_s` (default 24 h) —
   `cleared_reason='unavailable_timeout'`. The disk was removed, the service was
   uninstalled, the volume was unmounted. Without this clause, decommissioning a
   monitored thing leaves a permanent alert.

   **CORRECTED, and this was a design flaw in the clause rather than a
   code/spec mismatch.** It read "the **alert** is older than", and both halves
   of the implementation followed it literally, against `health_alert.opened_at`.
   That is the wrong clock. A real breach that has been alive and re-observed
   for ten days is *already* older than any legal value of the setting, so the
   **first ever** `unavailable` summary for it cleared it instantly — and
   cleared it as `unavailable_timeout`, the one reason §13 requires reporting to
   exclude from resolution counts. The finding left the open list and never
   appeared in the resolved list: it vanished from both sides of the ledger,
   which is the exact outcome the reason enum exists to prevent. The measurement
   is now `health_alert.last_seen_at` — stamped when the alert opens, bumped by
   every duplicate breach — i.e. "how long since anything reconfirmed this",
   which is what "the sensor has been gone long enough to call it removed"
   actually means. For an alert that never reconfirms, `last_seen_at` equals
   `opened_at` and the behaviour is unchanged, which is why the original tests
   could not see the difference: every one of them opened the alert at `now`;
4. the check is removed from the profile, disabled, or the device's assignment
   changes such that the check no longer applies —
   `cleared_reason='policy_removed'`.

   **All three limbs are implemented.** They were not: as first built, only
   removal-from-the-profile was, because the clear ran off the SET DIFFERENCE in
   `profiles.replace_checks` and a check that is still in the profile — merely
   `enabled: false`, or merely no longer resolving to this device — never
   reached it. Its alerts then survived every subsequent report, since the check
   had left `agent_checks` and every line naming it was discarded at §6 step 1.
   `replace_checks` now covers removal and disabling,
   `create_assignment`/`delete_assignment` cover the assignment change, and
   `update_profile` covers `active=false`, which §3 makes a fourth way for a
   check to stop applying. All four route through one predicate,
   `profiles.clear_alerts_for_inapplicable_checks`, which asks
   `resolve_profile_for_device` what applies NOW rather than reimplementing the
   limbs — so a fifth way to stop a check applying is covered the day it is
   added to `_check_applies`, not the day someone remembers this clause;
5. the device leaves `devices.status = 'active'` —
   `cleared_reason='device_inactive'`;
6. a user with `health.alert.manage` closes it —
   `cleared_reason='manual'`, with `cleared_by`.

`cleared_reason` is a CHECK-constrained enum over exactly those six values.
Which clause fired matters: an alert cleared by `recovery` is a fixed problem;
an alert cleared by `unavailable_timeout` is a **removed sensor** and must not
be counted as a resolution in any MTTR figure. Reporting is required to
distinguish them (§13).

**The clause deliberately NOT present:** an alert does **not** clear because the
device went offline. Offline is not healthy. Without this exclusion, a machine
that crashes under memory pressure "recovers" every one of its alerts at the
instant it dies, and the fleet looks healthiest at exactly the moment it is not.
The `agent.offline` check (§0.6) opens its own alert alongside them.

**States:** `active`, `acknowledged`, `muted`, `cleared`. There is no
`suppressed` state — see §0.9.

- **Acknowledge** (`active → acknowledged`): a named user takes ownership.
  Suppresses re-notification and escalation. Does **not** stop the check, does
  **not** prevent auto-clear, and does **not** cancel a remediation already in
  flight.
- **Mute** (`active|acknowledged → muted`) requires a `muted_until`, bounded to
  **30 days** by CHECK. A mute is time-boxed on purpose: unbounded mutes are how
  a fleet accumulates permanently silent alarms that everyone believes are
  still armed. On expiry the sweep returns the alert to `active` and re-notifies
  **once**.

### 0.9 Flap suppression suppresses actions, never evidence

Per alert key, `health_flap_state` keeps `window_start` and `open_count` (one
row per key; no history). On each candidate open:

```
if now - window_start > flap_window_s:      window_start = now; open_count = 0
open_count += 1
if open_count > flap_threshold:
    suppressed_until = now + flap_cooldown_s
    open the alert, set flapping = true, and DO NOT fire its actions
    refuse further opens for this key until suppressed_until
```

Tunables, org-level, all bounded by CHECK: `flap_window_s` (default 3600, range
300–86400), `flap_threshold` (default 4, range 2–50), `flap_cooldown_s`
(default 3600, range 300–86400).

**The alert record is still written.** Suppression removes the *actions* — the
mail, the ticket, the remediation job — not the evidence. A design that
suppresses the record turns "this device flapped 40 times overnight" into "it
was fine", which is the story an operator will believe because nothing
contradicts it. `flapping` and `flap_count` are columns on the alert precisely
so the console can sort by them; a flapping device is a *finding*, and often a
more important one than any single breach.

Rejected: **backoff on notification only** — the remediation job still fires
every cycle, so the restart loop continues and only the human stops hearing
about it. Rejected: **per-check hysteresis bands** (a separate clear threshold)
— it doubles the config surface for per-check tuning nobody performs, and
`for_ticks` (§0.3) already supplies the primary hysteresis at zero config cost.

The two mechanisms compose and both are required: `for_ticks` is edge-side and
free and kills sample noise; flap suppression is server-side and kills genuine
oscillation over minutes.

**Amendment — the cooldown must have an exit, and the pseudo-code above does not
give it one.** The block leaves the ordering of the cooldown against the window
unstated, and the wrong ordering is a real defect rather than a stylistic one:
if `suppressed_until` lapses while `open_count` is still above `flap_threshold`,
the very next open re-suppresses immediately and the "cooldown" becomes a
permanent silence nobody chose — the same class of failure as §0.8's mode (a),
an alert that can be opened but never cleared, moved one level up. The order
implemented in `app/modules/health/flap.py` is:

1. **still inside `suppressed_until`** → count the open (the evidence still
   accrues; the console sorts by `flap_count`) and refuse its actions;
2. **`suppressed_until` has lapsed** → clear it **and restart the window**
   (`window_start = now`, `open_count = 0`). Clearing only the flag is the
   mutation that produces the permanent silence above;
3. **not suppressed** → §0.9's block verbatim, with `>` in both comparisons: an
   open at exactly `flap_window_s` is still inside the window, and an open at
   exactly `flap_threshold` does not suppress.

### 0.10 Three actions, fixed order, executed after commit, idempotent by index

A check may declare up to one of each action kind: `remediate`, `open_ticket`,
`notify`. **Where that declaration is stored: three columns on `health_check`
(§2.2's third amendment) — `declares_notify`, `declares_open_ticket` and
`remediate_approved_action_id`.** "Up to one of each" is enforced by there being
exactly one slot per kind, so a second `notify` on one check is unrepresentable
rather than rejected. They **compose**, and their order is **fixed, not
configurable**:

**`remediate` → `open_ticket` → `notify`.**

Why that order: remediation is asynchronous and its job id is context the other
two want to carry ("we saw X and have already dispatched Y"); the ticket must
exist before the notification, so the mail can name a ticket number that
resolves; and notify is last because it is the only one with an **external,
irreversible** side effect — if ticket creation fails, sending nothing beats
sending a mail that points at a ticket which does not exist.

**Execution is deferred to a worker, not inlined.** The alert transaction writes
the alert row plus one `health_alert_action` row per configured action in state
`pending`, and publishes the outbox event — all in one transaction. A sweep
(§12) executes the pending rows. This is not a preference; it is what the
existing code requires: `app/modules/deploy/scripts.py::run_script` explicitly
does **not** publish to Redis inside the caller's transaction ("a job published
before the caller's tenant transaction commits could be handed to an agent with
no durable server record if the commit later failed"), and
`app/core/events.py` gives the identical reasoning for the outbox.

**Idempotency is two unique indexes, not application logic:**

- `uq_health_alert_open_key` (§0.8) — one open alert per key;
- `UNIQUE (alert_id, kind)` on `health_alert_action` — one ticket, one
  remediation dispatch, one notify, per alert, forever.

An at-least-once evaluation — a redelivered breach message, a re-run sweep, a
summary that repairs a missed edge, a worker that dies mid-action and retries —
therefore produces **one** ticket, not one per tick. Every retry path lands on
one of those two indexes.

A failed action row carries `attempts`/`next_attempt_at`, retries on the
notification module's backoff shape, and ends `dead`. A `dead` remediation does
not block the ticket or the notification: the actions are independently
retryable and independently terminal.

### 0.11 The caps are database constraints, because guidance does not hold

Three numbers make §0.5's idle claim checkable, and all three are CHECK
constraints rather than documentation:

- **`health_profile.tick_interval_s ≥ 60`** (default 300, max 3600).
- **at most 32 `health_check` rows per profile**, enforced by a statement-level
  trigger on insert (a CHECK cannot count siblings).
- **`health_profile.posture_interval_s ≥ 900`** (default 3600), and
  `posture_interval_s ≥ tick_interval_s`.

A 5-second tick with 400 checks is how an idle budget dies, and it dies quietly
because nobody re-measures after a config change. These bounds are the only
reason §0.5's claim survives contact with a customer.

### 0.12 What this module retains, and why none of it is a time series

Exactly three tables **retain** anything. "Retain" is the load-bearing word and
it means *accumulate rows over time*: the module owns nine tables (§2), but the
other six are configuration (`health_profile`, `health_check`,
`health_profile_assignment`, `health_org_settings`), per-alert bookkeeping
(`health_alert_action`, bounded by `UNIQUE (alert_id, kind)`), or one row per
device (`device_health_policy_ack`, §2.11 — same shape and same bound as clause 2
below). §0.8's amendment reaffirms this list against the pressure to add a
counter table, and nothing in §2 may be added to it without amending this
sentence first.

1. **`health_alert`** — one row per *opened alert*. This is an **incident
   record**: rows are created only on a state transition caused by a
   human-meaningful event, at a rate bounded by §0.9's flap suppression, and
   each is joined to a ticket and a job. A healthy 500-device fleet produces
   thousands of rows a year, not millions of samples. Retained per
   `alert_retention_days` (default 400, CHECK ≥ 90 — long enough to answer "has
   this happened before?" across an audit period).
2. **`device_health_check_state`** — exactly **one row per
   `(device_id, check_id, selector)`**, UPSERTed by every summary.
   `updated_at` moves; nothing accumulates. Cardinality is bounded by
   devices × 32 and is **constant in time**.
3. **`health_flap_state`** — one row per alert key. Same shape, same bound.

**The boundary, expressed as a property rather than a promise:** *"what was this
device's CPU last Tuesday?" must be unanswerable from this module's schema.* It
is, because no table stores a value with a timestamp that is not overwritten.
This is pinned by a test (§14): run 100 summaries for one stable device and
assert every row count is unchanged.

Explicitly **not** here, ever, per `docs/06-roadmap.md`'s Phase 16 note: streamed
metrics, sample history, retention tiers or downsampling, SNMP, network
topology, agentless/synthetic checks, and any chart with a time axis. When the
question is "why did memory climb over six hours", the answer is the monitoring
product. The alert detail view renders a deep link built from
`health_org_settings.monitoring_deep_link_template` (a URL template with
`{hostname}`); the **server never fetches it** — it is rendered as a link in the
browser, so it is not an SSRF surface and does not touch `app/core/egress.py`.

---

## 1. Purpose & boundary

### 1.1 In scope

Threshold-based endpoint health evaluated at the edge (§4), an alert lifecycle
with dedup, flap suppression and auto-clear (§6), and three alert-triggered
actions — notify, open a ticket, run an approved remediation as a signed job
(§7, §8) — with tenancy, RBAC and audit per invariants 4/5/6 (§11).

### 1.2 Boundaries

**vs. the monitoring product** — §0.12. Non-negotiable and structural.

**vs. the job engine** — this module calls `JobEngine.create()` and
`publish_job()` unmodified. It registers job types through
`app/jobs/registry.py` like every other module. It does not touch signing,
canonical bytes, the dispatch path, or the agent-side runner.

**vs. notification** — this module **publishes** four event types to the kernel
outbox (`app/core/events.py`) and ships no mail, no webhook, no channel config
and no delivery retry. Recipients for an alert are an ordinary notification
rule. §9 states the obligations that registering an event type incurs, because
they are enforced by tests, not by convention.

**vs. the ticket core** — this module calls `tickets.create_ticket(...,
origin="alert", source_alert_id=...)` and `transitions.change_status(...,
actor_type="system", actor_id=None)`. It defines no ticket status, no
transition, and no second answer to who may do what. Both entry points already
exist and already validate this contract — see §15.

**vs. inventory** — this module reads `devices` and `agents`. It deliberately
does **not** read inventory's posture facts; see §15 for why the brief's
assumption does not survive the code.

### 1.3 Pull-forward note

`docs/06-roadmap.md` recommends an early-mid real position for this module,
possibly ahead of Service Desk. If it is built before the ticket core exists,
exactly two things defer and nothing else changes:

- the `open_ticket` action kind (its `health_alert_action` rows are simply never
  created; the enum value exists);
- `health_org_settings.default_alert_queue_id` (nullable, FK to `ticket_queue`).

If it is built before the **notification core**, the `notify` action defers too
and this module is alert-console-only. The remediation loop — the part with the
most product value and the most risk — has no dependency on either.

---

## 2. Data model

**The migration number is confirmed at build time and is deliberately not stated
here.** An earlier draft named `0040` against a head of `0039`; the head moves
under concurrent work — `0039_sla_breach_sweep_indexes.py` was itself in flight
from another agent while this spec was written and reached disk before the spec
did. Before writing each migration, run `alembic heads` and `ls
migrations/versions/`, take the next free number, and set `down_revision` to the
bare short string of the **actual** head. Any number written in a document is
stale by the time it is read; treat a hardcoded one as a defect, not a default.
The schema may land as one migration or several (the implementation plan splits
it three ways — kernel events/permissions, the `approved_action` registry, the
health tables); that is a build-time choice and this spec does not fix it.

**As built, recorded after the fact and not before it.** The rule above is about
*writing* a number into a document a build will later read; once the migrations
exist, their numbers are facts and a reader who has to `ls migrations/versions/`
to find them is being made to re-derive something. This module shipped as
**four** migrations, chained: `0040_health_kernel` (the five permission codes,
their role grants, and the four `notification_rules` staff-only CHECKs),
`0041_approved_actions` (§2.9 + §2.10, both kernel-owned), `0042_health` (the
nine module tables of §2.1–§2.8 and §2.11), and — a full sub-project later, after
§7 was built and the gap was found — `0044_health_check_action_declaration`
(§2.2's three action-declaration columns). `0043` is IPAM's, from a concurrent
agent, which is the concrete instance of why no number was written here in
advance.

**Eleven new tables: nine owned by this module (§2.1–§2.8, §2.11) and two owned
by the kernel (§2.9, §2.10).** **The count is still nine and eleven after §2.2's
third amendment:** §0.10's per-check action declaration — the one thing §2
originally enumerated no home for — landed as three columns on `health_check`
rather than as a tenth table, for the reasons recorded there. Every one is
org-scoped, carries `org_id`, and
gets RLS **`ENABLE + FORCE`** with a `tenant_isolation` policy carrying **both**
`USING` and `WITH CHECK` — invariant #5 — with a behavioural cross-org test per
table (§14). Only three of them retain rows over time; see §0.12.

### 2.1 `health_profile`

`id (uuid7 PK), org_id FK, name (str 200), description (text), tick_interval_s
(int, default 300), posture_interval_s (int, default 3600), summary_interval_s
(int, default 1800), active (bool, default true), policy_version (int, default
1), created_by FK NULL, created_at, updated_at`.

- Unique on `(org_id, name)`.
- CHECKs: `tick_interval_s BETWEEN 60 AND 3600`;
  `posture_interval_s >= 900`; `posture_interval_s >= tick_interval_s`;
  `summary_interval_s BETWEEN 300 AND 86400` (§0.11).
- `policy_version` increments on **any** write to the profile or its checks. It
  is what §0.4's reconciliation compares and what §0.8's open predicate tolerates
  one version of drift on. It is server-owned; no API accepts it.

### 2.2 `health_check`

`id, org_id FK, profile_id FK, check_type (str 40), selector (str 200, default
''), comparator (str 4), threshold_num (numeric NULL), threshold_bool (bool
NULL), for_ticks (int, default 3), severity (str 10), os_family (str 8 NULL),
enabled (bool, default true), declares_notify (bool NOT NULL, default true),
declares_open_ticket (bool NOT NULL, default true),
remediate_approved_action_id (uuid NULL FK approved_action ON DELETE RESTRICT),
retired_at NULL, created_at, updated_at`.

- **Third amendment: the three action-declaration columns (§0.10), added by a
  later migration than the one that created this table.** §0.10 has always said
  "a check may declare up to one of each action kind", §7.3 turns that into the
  difference between an alert that mails a team and one that is console-only,
  and §6 makes escalation skip a check with no `notify` — but this column list
  originally ended at `retired_at` and **nothing in §2 related a check to an
  action**. The build that implemented §7 therefore had to derive a declaration
  in which every check notified and no check remediated, and recorded the gap
  behind a forcing constant rather than inventing a home for it. These three
  columns are that home; the constant and its test are gone.

  **Why columns and not a tenth `health_check_action` table** — the shape was
  chosen against how the data is read, and the reasons are recorded because the
  shape is the decision. (1) "Up to one of each kind" becomes *unrepresentable*
  rather than rejected: one slot per kind needs no `UNIQUE (check_id, kind)` to
  get right and no partial index to fall into §0.2's NULL-collision trap, which
  has now caught this schema twice. (2) Both call sites —
  `for kind in declared_action_kinds(check, settings)` and
  `if "notify" in declared_action_kinds(check, settings)` — are already holding
  the loaded check row; a child table would add a query per alert open and one
  per escalation candidate, and §12's escalation sweep already loads the check
  one alert at a time. (3) **§2's table count therefore does not move: nine
  module tables, eleven in total.** §0.12 requires that any addition to §2 be
  argued against its retention census first; three columns on a table already
  classified there as *configuration* mean the argument does not arise, whereas
  a tenth table would have to be defended as non-retaining.

- **`remediate` is declared by naming an approved action, not by a boolean, and
  the asymmetry is deliberate.** `health_alert_action.approved_action_id` is
  what §7.1's dispatcher resolves, and the worker marks a `remediate` row
  `skipped` when it is NULL — so a `declares_remediate` boolean would mint rows
  whose only possible outcome is `skipped`, and would leave "which approved
  action answers this check?" unanswered in the schema exactly as it was
  before. Declaring remediation and naming the action it dispatches are one
  act. The column is **NULL by default**, so remediation is off for every row
  that existed before the migration and every row created after it until a
  human names an action (invariant #1), and naming one is only the first gate:
  §7.1's refusals, §2.9's fuses, §8.3's dial and
  `ck_approved_action_autonomous_is_bounded` are all still in front of a
  dispatch. The FK is `ON DELETE RESTRICT`, matching `health_alert.check_id`;
  `delete_approved_action` refuses first and names the blocking checks, so the
  `IntegrityError` is the backstop rather than the message.

- **Retiring a check disarms it** — `_retire_or_delete_checks` sets
  `remediate_approved_action_id` back to NULL along with `retired_at`. A retired
  check is excluded from resolution, from every push and from the
  effective-profile view, so it can never open another alert and its declaration
  is dead data; but with the FK at `RESTRICT`, leaving it would make the
  approved action it names **permanently undeletable**, blocked by a row the
  operator can no longer see or edit. No history is lost — what was actually
  dispatched lives in `health_alert_action` and `jobs`.

- **`delete_approved_action` has a second refusal, and it predates these
  columns.** `health_alert_action.approved_action_id` is also a blocking FK, and
  it had no message in front of it: deleting an action that had ever been
  dispatched surfaced as a raw `IntegrityError` naming a constraint and no
  alerts. It is a genuine block (§2.4: history is not destroyed because an admin
  deleted a config row) and it clears itself when §12's retention sweep ages the
  alerts out — the same shape `delete_profile` already documents. It is now
  refused by name, with `active=false` given as the path meanwhile. Arming a
  check makes this path far easier to reach, which is why it is named here.

- **Both booleans default `true`,** which reproduces the derived behaviour every
  pre-existing row had, so the migration changes no behaviour on its own.
  `declares_open_ticket` remains ANDed in the service with the org having
  configured both `default_alert_queue_id` and `default_ticket_category_id`:
  that half is not the operator's to override, because `create_ticket` requires
  a category and resolves a queue or raises, so without both the action could
  only ever reach `dead`.

- **The FK is org-scoped by the service, not by the database.**
  `remediate_approved_action_id` is a plain FK and a Postgres foreign-key check
  does not consult RLS, so the FK alone would let one org arm a check against
  another org's approved action — a command path into a fleet that never
  approved it. `profiles._validate_declared_remediations` resolves the id
  `WHERE org_id = :org`, which is the same hole and the same answer as
  `_reject_cross_org_target` for `device_id`. It also refuses an action lacking
  the `health` persona at write time (§8.2's posture), because every remediation
  such a check produced would otherwise be `skipped` at dispatch.

- **The API takes and returns the same identifier.** `CheckIn` accepts
  `remediate_approved_action_id` rather than an action *key* precisely because
  §10 replaces checks as a whole set: a console that GETs the set and PUTs it
  back must round-trip the identifier the read side returned, or an unrelated
  edit silently disarms a remediation.

- `check_type` is CHECK-constrained to the registry in §4.1. `comparator` is
  CHECK-constrained to `gt|gte|lt|lte|eq|neq|is`.
- Exactly one of `threshold_num`/`threshold_bool` is non-NULL, matched to the
  check type's `value_kind` — a CHECK plus a service-layer validation against
  the registry, because SQL cannot consult the registry.
- `for_ticks BETWEEN 1 AND 60`; `severity IN ('info','warning','critical')`;
  `os_family IN ('windows','linux','macos')` or NULL (= any).
- Unique on `(profile_id, check_type, selector, os_family)` — the same check
  against the same selector twice in one profile is a config mistake, not two
  checks. **Amendment: this is a *partial* unique index,
  `WHERE retired_at IS NULL`.** A retired check (see §2.4's amendment) keeps its
  row forever; a non-partial index would make re-adding a check an operator once
  removed permanently impossible, which is the failure the retire mechanism
  exists to avoid rather than cause. **Second amendment: the index is also
  `NULLS NOT DISTINCT`.** `os_family` is nullable and NULL means "any OS", and
  NULLs do not collide in a Postgres unique index — so without it the *most
  common* shape of check (`cpu.busy_pct`, no selector, any OS) is storable twice
  in one profile and pushed twice to every device. This is §0.2's NULL-collision
  trap arriving on a different table, and the plain `UNIQUE` this bullet
  originally described would have enforced nothing for exactly the rows
  operators write most.
- A statement trigger enforces the 32-row-per-profile cap (§0.11). **Amendment:
  the trigger counts `WHERE retired_at IS NULL`** — retired rows are history, not
  policy, and counting them would let a profile become unusable at 32 lifetime
  checks instead of 32 live ones. The plan's multi-row-insert test for this
  trigger must therefore also cover a profile with retired rows present.
  **Correction, verified against PostgreSQL 16 rather than assumed:** the reason
  for statement level is *cost*, not multi-row correctness. A `FOR EACH ROW`
  trigger (BEFORE or AFTER) counting siblings **does** see rows the same
  statement already inserted, because PL/pgSQL increments the command counter
  before each of its own statements — both mutations were run and the multi-row
  test stayed green. Statement level is still correct: one count per statement
  instead of one per row, which matters on the 32-row `replace_checks` path.
- **Amendment: there are two cap triggers, INSERT and UPDATE**, over one
  function. Transition tables cannot be shared across trigger events, and
  without the UPDATE one, clearing `retired_at` on twenty rows is an unpoliced
  route back over the cap.
- `retired_at` is server-set, never accepted from a request. A retired check is
  excluded from resolution (§3), from every policy push (§5.2), and from the
  effective-profile view (§13).

### 2.3 `health_profile_assignment`

`id, org_id FK, profile_id FK, device_id FK NULL, device_group_id FK NULL,
created_by FK NULL, created_at`.

Three partial unique indexes and the mutual-exclusion CHECK, exactly as written
in §0.2.

### 2.4 `health_alert`

`id, org_id FK, device_id FK, check_id FK, profile_id FK, selector (str 200),
check_type (str 40), severity (str 10), state (str 14), observed_value (str 64),
threshold_display (str 64), opened_at, opened_policy_version (int),
last_seen_at, occurrence_count (int, default 1), flapping (bool, default false),
flap_count (int, default 0), escalated_at NULL, acknowledged_at NULL,
acknowledged_by FK NULL,
muted_until NULL, muted_by FK NULL, cleared_at NULL, cleared_reason (str 24)
NULL, cleared_by FK NULL, ticket_id FK NULL, previous_alert_id FK NULL (self),
created_at, updated_at`.

- `uq_health_alert_open_key` — the partial unique index from §0.8. **This is the
  single most important line in the schema.**
- CHECKs: `state IN ('active','acknowledged','muted','cleared')`;
  `cleared_reason IN ('recovery','summary_ok','unavailable_timeout',
  'policy_removed','device_inactive','manual')`;
  `(state='cleared') = (cleared_at IS NOT NULL)`;
  `(state='muted') = (muted_until IS NOT NULL)`;
  `muted_until <= opened_at + interval '30 days'`.
- `observed_value` is a **string**, deliberately: it must hold `"97.4"` and
  `"stopped"` and `"false"` without this table acquiring a numeric column that
  someone will later be tempted to aggregate over time (§0.12).
- `previous_alert_id` links to the last cleared alert for the same key within
  `flap_window_s`, so a re-opened alert's ticket can name its predecessor
  (§0.10's chronic-flapper case).
- **`escalated_at` (amendment).** Present in the column list above because §6
  requires it — "published once for an alert … stamped by `escalated_at`" — and
  an earlier draft of this section omitted it. That was a transcription bug, not
  a design decision. Without the column, §12's `health-alert-sweep` has no way to
  remember that it already escalated, and a sweep that re-fires on every pass
  turns one problem into an hourly mail. Stamped once, never cleared; an alert
  that returns from `muted` to `active` is **not** re-escalated, for the same
  reason.
- Indexes: `(org_id, state, severity)` for the console;
  `(org_id, device_id)`; partial `(org_id, opened_at) WHERE state <> 'cleared'`
  for the escalation sweep.

**Amendment — `check_id` is NOT NULL, and checks are retired rather than
deleted.** §10 promises that deleting a check leaves existing `health_alert` rows
intact; §2.4 makes `check_id` a non-nullable FK. Both cannot hold against a hard
`DELETE`, and clearing the alerts first does not help — a cleared alert still
carries `check_id` and still holds the reference.

**Decision, in three parts:**

1. **The FK is `ON DELETE RESTRICT`.** The database, not application care, is
   what keeps an alert's `check_id` resolvable.
2. **Removing a check from a profile is a *retire*, not a row delete.** The
   set-replace path (`PUT /health/profiles/{id}/checks`), profile deletion, and
   assignment changes that drop a check all: clear the check's non-cleared alerts
   with `cleared_reason='policy_removed'` (§0.8 clause 4), stamp
   `health_check.retired_at`, bump `policy_version`, and re-push. The row stays;
   the policy does not.
3. **A hard `DELETE` is permitted only for a check that has never opened an
   alert**, where RESTRICT does not fire. When it does fire, the API returns
   **409 naming the blocking alerts** — the count and the ids of the most recent
   few — never a bare `IntegrityError`. §12's `health-retention` sweep deletes
   alerts past `alert_retention_days` and then deletes any retired check left
   with no alerts, so retired rows are garbage-collected rather than immortal.

Rejected: **a nullable `check_id`.** It is part of the dedup key
(`uq_health_alert_open_key`), and NULLs do not collide in a Postgres unique
index — a nullable dedup component reintroduces the exact problem §0.2's partial
indexes exist to prevent, on the one index the whole alert lifecycle rests on.

Rejected: **`ON DELETE RESTRICT` with no retire mechanism.** It is the honest
constraint and it is unusable: a check that has ever alarmed could not be removed
from its profile for up to `alert_retention_days` (default 400). An operator
facing that will not wait — they will disable the check and leave it in the
profile, consuming one of the 32 slots and one line of every console view
forever, which is a worse outcome reached by an obvious route.

**The cost, stated:** `retired_at` is a soft delete, and soft deletes leak into
every query that forgot them. Three places must carry `WHERE retired_at IS NULL`
and each is named where it lives: resolution (§3), the unique index and the
32-check trigger (§2.2). A fourth — the retention sweep — must carry the
opposite. The perverse incentive accepted: "retire" reads as reversible, and an
operator will ask to un-retire a check. It is not offered; re-adding the same
`(check_type, selector, os_family)` mints a new row with a new id, and therefore
a new dedup key, because an alert history that silently spans a removal and a
re-add is a history that lies about continuity.

### 2.5 `health_alert_action`

`id, org_id FK, alert_id FK, kind (str 12), state (str 20), approved_action_id
FK NULL, job_id FK NULL, ticket_id FK NULL, attempts (int, default 0),
next_attempt_at NULL, approved_by FK NULL, approved_at NULL, expires_at NULL,
last_error (str 500) NULL, created_at, updated_at`.

- `UNIQUE (alert_id, kind)` — §0.10's idempotency guarantee.
- CHECKs: `kind IN ('remediate','open_ticket','notify')`;
  `state IN ('pending','awaiting_approval','dispatched','done','skipped',
  'blocked_rate_limit','blocked_flapping','expired','dead')`.
- `job_id` is set only for `kind='remediate'` and is the **kernel** `jobs.id` —
  the audit trail of what actually ran lives in `jobs` + `audit_events`, not
  duplicated here.

### 2.6 `device_health_check_state`

`id, org_id FK, device_id FK, check_id FK, selector (str 200), state (str 12),
last_value (str 64), policy_version (int), reported_at, updated_at`.

- `UNIQUE (org_id, device_id, check_id, selector)`. **UPSERT only; no history
  row is ever inserted for the same key** (§0.12).
- `state IN ('ok','breaching','unavailable')` (§0.7).

### 2.7 `health_flap_state`

`id, org_id FK, device_id FK, check_id FK, selector (str 200), window_start,
open_count (int), suppressed_until NULL, updated_at`.

- `UNIQUE (org_id, device_id, check_id, selector)`. One row per key, upserted.

### 2.8 `health_org_settings`

`org_id PK/FK, flap_window_s, flap_threshold, flap_cooldown_s,
unavailable_clear_after_s, alert_retention_days, escalate_after_s,
offline_after_s, approval_ttl_s, default_alert_queue_id FK NULL,
default_ticket_category_id FK NULL, monitoring_deep_link_template (str 500)
NULL, auto_resolve_alert_tickets (bool, default true), updated_by, updated_at`.

Defaults and CHECK ranges as given in §0.8/§0.9; `escalate_after_s` default 3600
(range 300–604800); `offline_after_s` default 900 (range 300–86400);
`approval_ttl_s` default 86400.

**Amendment — the three ranges this section left open, fixed in migration
`0042`.** §0.8/§0.9 give defaults for `unavailable_clear_after_s` (86400) and
`approval_ttl_s` (86400) and a lower bound for `alert_retention_days` (90), but
no range for any of the three. Every other tunable here is CHECK-bounded, and an
unbounded one is not a weaker version of a bounded one — it is the setting that
silently disables the behaviour it configures. Fixed as:

- `unavailable_clear_after_s BETWEEN 3600 AND 2592000` (1 hour – 30 days).
  Below an hour, §0.8 clause 3's "removed sensor" clear races an agent that is
  merely rebooting and starts attributing real findings to
  `unavailable_timeout`, which §0.8 says must never be counted as a resolution.
  Above a month the alert is retention, not signal.
- `approval_ttl_s BETWEEN 300 AND 604800` (5 minutes – 7 days), matching
  `escalate_after_s`'s range. A `require_approval` remediation whose TTL is
  hours long is already answering a situation that has changed (§7.1's reason
  for blocking rather than queuing a rate-limited remediation).
- `alert_retention_days BETWEEN 90 AND 3650`. The upper bound is new: without
  one, `alert_retention_days: 70000` disables retention entirely and looks
  identical to retention being broken — the same failure
  `docs/modules/sla/spec.md`'s `MAX_AUTO_CLOSE_AFTER_DAYS` exists to prevent.

### 2.9 `approved_action` — the shared registry

**This table is kernel-owned, not health-owned.** It lives in
`backend/app/core/approved_actions.py` with its model beside the other kernel
models, for exactly the reason `app/core/events.py`'s own docstring gives for
the outbox: *"This lives in `app/core/` rather than in
`app/modules/notification/` because Phase 16 publishes to the same facility. A
module registers into this; it does not own it."* Phase 17 and the AI-automation
layer must share this registry (`docs/06-roadmap.md`: "one registry, one dial,
one audit"), and a registry owned by the health module is a registry the AI
layer will fork.

`id, org_id FK, key (str 100), name (str 200), description (text), job_type
(str 100), params_template (jsonb), script_id FK NULL, script_version (int)
NULL, risk_class (str 8), destructive (bool), max_target_scope (str 8),
autonomy (str 16), personas (jsonb array of str), cooldown_s (int, default
3600), max_per_device_per_day (int, default 3), max_per_org_per_hour (int,
default 20), active (bool), created_by FK, created_at, updated_at`.

- Unique on `(org_id, key)`. **Org-scoped with no global rows** — a global
  approved action would be a cross-tenant command path into every fleet.
- `job_type` is validated at write time against
  `app.jobs.registry.registered_names()`. An unregistered type is a 400 at the
  API, never an `UnknownJobType` discovered at dispatch time with an alert
  already open.
- `script_id`/`script_version` are **required** when `job_type='script.run'` and
  forbidden otherwise (CHECK). Version pinning is the point: `scripts` is
  versioned (`uq_scripts_org_name_version`), and without pinning "approve the
  disk-cleanup script" silently means "approve whatever that script says
  tomorrow", which makes the approval worthless.
- `personas` is the **persona dimension Phase 17 requires**, present from day
  one. v1 rows carry `["health"]`; the AI layer adds `"end_user"`,
  `"technician"`, `"admin"` to **the same rows**. No parallel table, no fork.
- CHECKs: `risk_class IN ('low','medium','high')`;
  `autonomy IN ('suggest','require_approval','autonomous')`;
  `max_target_scope IN ('device')` **in v1** — the column exists so Phase 17 can
  widen it via a reviewed migration rather than by adding a concept.
- **The hard gate** (§8.3), as a database constraint:

  ```sql
  CHECK (NOT (autonomy = 'autonomous'
              AND (destructive OR risk_class = 'high' OR max_target_scope <> 'device')))
  ```

- `health_action_dispatch_log` is **not** a new table: the rate fuses
  (`cooldown_s`, `max_per_device_per_day`, `max_per_org_per_hour`) are evaluated
  against `jobs` — `WHERE jobs.org_id = ? AND jobs.type = ? AND jobs.target_id =
  ? AND jobs.created_at > ?` — because `jobs` is already the durable, audited
  record of every dispatch and a second counter would be a second truth.

**Amendment — what counting `jobs` by `type` actually costs.** The decision
stands; the consequences below were previously discoverable only in production
and are now stated. **The fuse is per `(org_id, job_type, device)`, not per
approved action.**

1. **Two approved actions sharing a `job_type` share one fuse.** This is not an
   edge case: §15.2 records that there is no service-restart job type and no
   cleanup job type, so *every* script-based remediation is `script.run`. "Restart
   the stuck service" and "clean the full disk" on the same device therefore
   compete for one `cooldown_s` and one `max_per_device_per_day` budget, and the
   second one to fire is the one that lands in `blocked_rate_limit`.
2. **Operator-initiated jobs consume the remediation budget.** A technician who
   runs three scripts by hand on a device with `max_per_device_per_day = 3` has
   spent that device's remediation budget for the day. Symmetrically, a
   remediation storm makes the technician's next manual run look like the
   remediation's fault when they read the audit log.
3. **`jobs.requested_by IS NULL` does not fix this, and must not be added.**
   Verified against the code: `app/worker.py`'s patch-scan sweep creates jobs with
   `requested_by=None`, so the predicate would sweep in unrelated system jobs;
   and a remediation dispatched after `require_approval` (§8.3) carries the
   **approver's** user id, so the predicate would exclude approved remediations
   from their own fuse. It is wrong in both directions.

Rejected, again and for the record: **a second counter table.** The fuse that
carries the weight is `max_per_org_per_hour` (§8.4 mechanism 3), and what it
bounds is *jobs arriving at the endpoint fleet* — a blast-radius bound, not an
accounting of which subsystem asked. Over-counting is the safe direction: a fuse
that counts a technician's manual runs fails **closed**. A per-action counter
would fail open on exactly the case that matters, the estate-wide trip.

**The cost, stated, and the perverse incentive accepted:** an operator whose
remediation was blocked cannot tell from the number alone whether another alert,
another approved action, or their own manual run consumed the budget. Mitigation,
**required in v1**: `health_alert_action.last_error` on a `blocked_rate_limit`
row names the fuse that tripped, its window, and the **job ids** counted against
it, so §13's alert detail can show what spent the budget. The incentive this
accepts is that the obvious response to a nuisance block is to raise
`max_per_device_per_day` — which raises the auto-remediation blast radius, the
exact thing the number exists to hold down. The mitigation is that the reason is
legible, so the operator raising it can see they are paying for a technician's
afternoon, not for a runaway loop.

### 2.10 `approved_action_script_exemption` — kernel-owned, keyed to a version

**Amendment.** §8.4 clause 2 takes `script_exemption` as a parameter of
`is_destructive` and §10 exposes `POST /approved-actions/{id}/exempt-script`, but
no earlier section said where an exemption lives. It lives here.

**This table is kernel-owned**, beside `approved_action` in
`app/core/approved_actions.py`, for the same reason §2.9 gives: Phase 17 shares
the derivation function, so it must share the row the function reads.

`id, org_id FK, script_id FK (→ scripts.id, ON DELETE CASCADE), script_version
(int), reason (str 500), granted_by FK, granted_at`.

- **`UNIQUE (org_id, script_id, script_version)`.** Keyed to the **pair**, not to
  an approved action. §8.4's own wording — "granted per exact version … editing
  the script mints a new version and the exemption does not follow it" — is a
  property of a script version, and two approved actions naming the same pinned
  script version share one exemption rather than needing two identical grants.
- Org-scoped, **no global rows**, RLS `ENABLE + FORCE` with `tenant_isolation`
  carrying both `USING` and `WITH CHECK`. A cross-tenant exemption would be a
  cross-tenant path to `autonomous` execution of arbitrary code.
- `ON DELETE CASCADE` on `script_id`: deleting a script must not leave an
  exemption a later script could inherit by id reuse.
- Granting one requires `health.action.admin` and is audited
  `health.action.destructive_exempted` with `(script_id, script_version)`
  (§11.3). Revocation is a row delete, audited the same way.

**Why the pair and not `script_id` alone — verified against the code, and it
matters.** `app/modules/deploy/scripts.py::update_script` bumps `version`
**in place on the same row**, with a docstring stating the reason. A script's
`id` is therefore *not* a version identifier: the same id's body changes under
it, and **the previous body is not retained anywhere**. Two consequences the
implementation must carry:

1. `(script_id, script_version)` is genuinely load-bearing on both
   `approved_action` and this table. An exemption keyed on `script_id` alone
   would silently follow every future edit — which is precisely the "approve
   whatever that script says tomorrow" failure §2.9's pinning exists to prevent.
2. Because the old body is gone, pinning a version cannot mean *fetching* the
   approved body. It means a **staleness check at dispatch time**: §7.1 compares
   `approved_action.script_version` against the live `scripts.version` and
   **refuses** the dispatch (`last_error` naming both versions, action row not
   retried) when they differ. It does not run the new body, and it does not
   silently re-derive `destructive`.

Rejected: **an `exempt` boolean on `approved_action`.** It would be per approved
action rather than per script version, so two actions pinning the same version
could disagree about whether that exact body is destructive — one derivation
function producing two answers, which is the thing §8.4 exists to make
impossible.

**The cost, stated:** an exemption survives the approved action that motivated
it. Deleting the action leaves the row, and a later action pinning the same
`(script_id, script_version)` inherits an exemption granted for a different
purpose. This is accepted rather than fixed because the alternative — expiring
exemptions with their actions — makes the same `(script_id, script_version)` pair
destructive or not depending on lookup order. The mitigation is visibility: the
exemption list is a first-class view (§13's approved-actions page) showing script
name, version, grantor and reason, and §13 renders the pinned script's name and
version on every approval prompt, so nobody approves a body they cannot identify.

### 2.11 `device_health_policy_ack` — one row per device, never a history

**Amendment.** §5.2's result handler "stamps `device_health_policy_ack`", §0.8
clause 2 compares against "the device's last *acknowledged* version", and §12's
reconciliation sweep re-pushes where that version is stale — but no earlier
section defined it. It is this table.

`org_id FK, device_id PK/FK, profile_id FK NULL, policy_version (int, default 0),
acknowledged_at, updated_at`.

- **PK is `device_id`; one row per device, UPSERTed.** Never inserted twice for
  the same device, so it is bounded by the fleet and **constant in time** — the
  same shape and the same bound as §2.6, which is why it does not breach §0.12.
- `UNIQUE (org_id, device_id)` alongside the PK, so every query in this module
  can carry its explicit `org_id` predicate against an index (invariant #5).
- RLS `ENABLE + FORCE`, `tenant_isolation` with `USING` and `WITH CHECK`, and a
  behavioural cross-org test like every other table (§14).
- **Two writers, both idempotent:** the `health.policy.apply` result handler
  (§5.2, promptness) and every `summary` line's `policy_version` (§4.2,
  correctness — the summary is the channel that survives a lost result). Last
  write wins; a lower `policy_version` never overwrites a higher one.
- `profile_id` is nullable and records *which* profile was acknowledged, so a
  reassignment is distinguishable from a version bump on the same profile. NULL
  means the device acknowledged a disarm (§5.2's empty `checks` list).
- **A missing row reads as `policy_version = 0`**, i.e. "never acknowledged".
  §0.8 clause 2 is then vacuously satisfied and §12 always re-pushes. Both are
  the right way to fail: a device that has never acknowledged a policy must not
  have its breaches discarded, and must not be assumed converged.

Rejected: **a `policy_version` column on `devices`.** `devices` is kernel-owned,
and a module that adds bookkeeping columns to a kernel table has forked the
kernel rather than registered into it (`CLAUDE.md`, build discipline 3). Phase 17
would then want its own, and the argument for refusing it would already be lost.

Rejected: **a row per acknowledgement.** That is a `policy_version` time series
keyed by device — a fourth retaining table and §0.12's boundary conceded for
bookkeeping.

**The cost, stated:** because nothing accumulates, this table cannot answer "when
did this device *start* falling behind" or "how many times has it failed to
converge". A device that acknowledges nothing forever produces one stale row and
a reconciliation sweep that re-pushes every 900 s indefinitely, with no
escalating signal. Accepted, with the mitigation named: `acknowledged_at` going
stale **is** the signal, §13's device-health view renders policy drift explicitly,
and a device that is not converging is a device whose `agent.offline` check (§0.6)
or whose job-failure record in `jobs` is the more informative artefact. The
perverse incentive this accepts is the pressure to add "just an attempts counter"
here; that counter is `jobs` filtered on `type = 'health.policy.apply'`, and it is
already durable and already audited.

---

## 3. Check-policy resolution

For a device, first match wins, most specific first:

1. `health_profile_assignment` where `device_id = device.id`
2. the nearest ancestor in `device_groups` (self first, then `parent_group_id`
   upward, depth-bounded at 16 — §0.2) with an assignment
3. the org default (`device_id IS NULL AND device_group_id IS NULL`)

Only `active` profiles are considered. **If nothing matches, the device has no
health profile** — no checks are pushed, no `health.policy.apply` job is
dispatched, and the console shows "no health profile", not "healthy". Following
`docs/modules/sla/spec.md` §3: a fabricated default is worse than a visible
absence, and "healthy" is the single most dangerous default a monitoring module
could pick.

*Amended 2026-09-23 (platform review): a seeded baseline, not a fabricated
default.* Resolution still invents nothing. What changed is that each org is
given, once, a real "Workstation baseline" profile assigned as its org default
(`app/modules/health/baseline.py`): an ordinary row written through this
module's services and audit chain, visible and editable, and never re-created
once deleted (`health_baseline_seed`, migration 0084). It is seeded at setup
claim and, for existing orgs, by the hourly `health-baseline-reconcile` beat;
an org that already has any profile is recorded `skipped` and left untouched.
Checks: `disk.free_pct` `*` < 10 (critical); `agent.offline` (warning, no
ticket; `offline_after_s` set to 1800 only when the org has no settings row);
`posture.disk_encryption` and `posture.firewall_enabled` on Windows and macOS
(critical); `process.present` `EPWD.exe` on Windows for the Check Point
Harmony Endpoint watchdog (critical). Critical checks declare `open_ticket`,
which takes effect once `default_alert_queue_id` and
`default_ticket_category_id` are set. No macOS Harmony row until the daemon's
process name is confirmed, and no `service.state` check because no agent
sampler answers it yet.

Within the resolved profile, a check applies to the device iff
`check.retired_at IS NULL AND check.enabled AND (check.os_family IS NULL OR
check.os_family = device.os_family)`. (`retired_at` per §2.4's amendment: a
retired check is history, and history is never pushed to an endpoint.) Filtering by OS happens **server-side, at policy-push
time**, so an agent is never shipped a check it cannot answer — that is one less
`unavailable` state to explain and one less shell-out to attempt.

Resolution runs when: a device enrols; a profile, check, or assignment is
written; a device's `device_group_id` changes; and on the reconciliation sweep.
Each run that changes a device's effective check set bumps
`health_profile.policy_version` and queues a `health.policy.apply` dispatch.

---

## 4. The `health.check` agent capability

### 4.1 The check-type registry (closed, v1)

`evaluated_by = agent` unless noted. `value_kind` fixes which threshold column
applies. `cadence` is `tick` (fast) or `posture` (slow sub-tick).

| `check_type` | value_kind | selector | cadence | windows | linux | macos |
|---|---|---|---|---|---|---|
| `cpu.busy_pct` | numeric 0–100 | — | tick | ✓ | ✓ | ✓ |
| `memory.used_pct` | numeric 0–100 | — | tick | ✓ | ✓ | ✓ |
| `disk.free_pct` | numeric 0–100 | mount/volume, `*` = worst | tick | ✓ | ✓ | ✓ |
| `disk.free_bytes` | numeric | mount/volume, `*` = worst | tick | ✓ | ✓ | ✓ |
| `service.state` | enum `running`/`stopped` | service name | tick | ✓ | ✓ | ✓ |
| `process.present` | boolean | process name | tick | ✓ | ✓ | ✓ |
| `disk.smart_ok` | boolean | device path, `*` = all | posture | ✓ | ✓ | — |
| `posture.disk_encryption` | boolean | — | posture | ✓ | ✓ | ✓ |
| `posture.antivirus_healthy` | boolean | — | posture | ✓ | — | — |
| `posture.firewall_enabled` | boolean | — | posture | ✓ | ✓ | ✓ |
| `eventlog.disk_error` | numeric (count since last tick) | — | tick† | ✓ | ✓ | ✓ |
| `eventlog.service_crash` | numeric (count since last tick) | — | tick† | ✓ | ✓ | ✓ |
| `eventlog.kernel_fault` | numeric (count since last tick) | — | tick† | ✓ | ✓ | ✓ |
| `agent.offline` | numeric (seconds since last seen) | — | — | **server** | **server** | **server** |
| `script.policy_failed` | boolean | schedule name, empty = any | — | **server** | **server** | **server** |

`script.policy_failed` (added 2026-09-23, migration 0121) is evaluated when a
scheduled script's run result arrives (`docs/modules/app-deploy/spec.md` §9.1):
a failed run opens an alert keyed by the schedule name, the next success clears
it. It is never pushed to an agent.

A `—` cell means the check reports `unavailable` on that OS (§0.7), which is
different from failing. `posture.antivirus_healthy` is Windows Security Center
only in v1; "AV health" on Linux/macOS has no vendor-neutral answer and
inventing one produces a check that is confidently wrong.

**Amendment, 2026-08-14 — what the `windows` ✓ has meant, and what backs it
now.** This table has marked `cpu.busy_pct`, `memory.used_pct`, `disk.free_pct`,
`disk.free_bytes` and `process.present` ✓ for `windows` since it was written —
the mark states that a check type is **legal to configure** on that OS, not
that an implementation exists or has run; §0.5 tracks implementation state
separately, and until this cycle those five had none on Windows
(`sampler_other.go` answered `unavailable` for all of them, same as macOS
still does). As of this cycle those five have an implementation,
`sampler_windows.go`, built on `GetSystemTimes`, `GlobalMemoryStatusEx`,
`GetDiskFreeSpaceEx` and Toolhelp's process snapshot — no cgo, no WMI, no
performance counters, no shell-out.

**Stated plainly so the ✓ is not read as more than it is: this code is
written and compile-checked, and has never executed.** `GOOS=windows go vet
./...` and `GOOS=windows go test -c` are the only checks it has ever passed.
No machine in this project can run it, and its numbers — a CPU percentage, a
memory percentage, two disk-free readings, a process-present boolean — have
never once been compared against Task Manager or `wmic`. `service.state`
keeps its ✓ for `windows` on the same legal-not-implemented basis it always
has; it is unimplemented on every OS and this cycle did not touch it.

**Amendment, 2026-08-14 — macOS, and the two checks deliberately left
unimplemented there.** The `macos` column is unchanged by this cycle and needs
no edit, for the reason the paragraph above gives: ✓ has always meant *legal to
configure*. What changed is implementation state, which §0.5 tracks.

`disk.free_pct`, `disk.free_bytes` and `process.present` now have a macOS
implementation, `sampler_darwin.go`, built on `unix.Statfs`, `unix.Getfsstat`
and `unix.SysctlKinfoProcSlice` — no cgo, no shell-out. `cpu.busy_pct` and
`memory.used_pct` **remain unimplemented on macOS and this is a recorded
refusal rather than a to-do.** Both require Mach (`host_statistics64`,
`vm_statistics64`), `golang.org/x/sys v0.33.0` exposes no Mach interface at
all, and the agent is a cgo-free static binary.

The reason it is a refusal and not a deferral is worth the sentence, because
"hand-roll the libSystem binding" is the obvious next move: a probe using
x/sys's own no-cgo pragmas for `host_statistics64`, with the required assembly
trampoline **omitted entirely**, still passed `GOOS=darwin go build` cleanly.
On a real Mac that code calls through a null function pointer. So the binding
*mechanism* — the part most likely to be wrong — is invisible to every check
this project can run, and two checks shipped on that basis would report
`unavailable` forever on every Mac while looking finished. A macOS runner in
CI, or a deliberate move to cgo for darwin, would change that; neither is a
sampler cycle's decision.

**Stated as plainly as the Windows paragraph above: no macOS code in this
project has ever executed.** `GOOS=darwin go vet ./...` and, as of this cycle,
`GOOS=darwin go test -c` are the only checks it has passed — and the probe
above is precisely why a green darwin cross-compile must not be read as
evidence that anything works. What *is* executed is the shared calc layer
(`statfsFreeBytes`, `cstr`, `freeSample`, `worseSample`, `processNameMatches`),
which Linux CI runs.

Two cross-platform divergences macOS introduces are recorded in
`sampler_calc.go` beside the functions they affect, not here: `getfsstat`
double-counts APFS firmlinks (`/` and `/System/Volumes/Data` are one physical
store), and `P_comm` truncates process names at 16 characters where Windows
does not truncate at all — so a policy naming a long executable matches on
Windows and never on macOS or Linux.

† The `eventlog.*` types are **counters read from the security-logs capability's
existing OS subscription** (§0.5 item 5). They open no subscription of their
own; with security-logs disabled they report `unavailable`. The three condition
names are builtin and map to OS-native filters in the agent (`agent/`, unbuilt).
**Operator-
authored log queries are deferred (§17)** — a customer-supplied query is the
free-form expression §0.1 rejects, wearing a different hat.

**Amendment — where an `enum` check's threshold is stored, and where its
`cadence` comes from.** This table gives three `value_kind`s while §2.2 gives
`health_check` exactly two threshold columns, so `enum` has no column of its
own. Resolved without adding a third column (§2.2 fixes the column list) and
without dropping the `enum` kind (which would lose that the agent reports the
literal string `running`, per §5.1's example):

- an `enum` check type names **exactly two** members; `enum_values[0]` is the
  member `threshold_bool = true` denotes, and `threshold_bool` is the column
  used. `service.state` is therefore `("running", "stopped")`, and
  `threshold_bool = true` means "healthy is running".
- `registry.render_threshold` emits the **member name** on the §5.2 wire, not
  `true`/`false`, so the agent compares its reported `running`/`stopped` against
  the same alphabet rather than mapping a boolean back to one.
- a third member would need a schema change — which §0.1 already requires of
  any registry change, so the bound costs nothing that was not already paid.

Separately: `agent.offline`'s `cadence` cell is `—` above, but the field is
`Literal["tick","posture"]` in code and every consumer would otherwise need a
`None` case. It is recorded as `posture` and is **inert** — the type is
`evaluated_by = 'server'` and never appears in a policy push (§0.6), so nothing
reads it.

Comparator legality is per `value_kind` and lives in the registry, not in SQL:
numeric types take `gt|gte|lt|lte|eq|neq`, boolean and enum types take `is` and
nothing else. `ck_health_check_comparator` cannot express this — `gt` is a legal
member of the column's domain — so the registry plus §10's validation is the
only thing between "disk encryption > true" and an agent asked to evaluate it.

### 4.2 Tick behaviour

Per fast tick, for each applicable check: sample, compare, update the counter.

- counter reaches `for_ticks` and the check was not breaching → **emit `breach`**,
  set breaching;
- check was breaching and the comparison no longer matches (single sample —
  recovery is **not** duration-gated) → **emit `recovery`**, clear breaching;
- otherwise → **emit nothing**. This is the common case and it costs one
  comparison and one integer write.

Recovery being un-gated while breach is duration-gated is asymmetric on
purpose: a slow-to-open, fast-to-close alert errs toward silence, and the
oscillation this permits is what §0.9 exists for.

Posture checks run on the posture sub-tick and feed the same counters (a
`for_ticks` of 3 on a posture check therefore means three *posture* ticks —
surfaced in the UI, per §0.3).

Every `summary_interval_s`, the agent emits one **summary**: every applicable
check's current state, current value and the installed `policy_version`. This
is the reconciliation channel (§0.8 clauses 2 and 3, §0.4's version compare) and
it is the only periodic network write.

### 4.3 Between ticks

Nothing. No goroutine, no open handle to a performance counter, no cached
sample set. The capability is a timer callback and a fixed-size state array.
§0.5 states the claim and its falsifiers.

---

## 5. Wire contract

### 5.1 Agent → server: `health_report`

A **new `AgentMessage` variant** in `app/gateway/protocol.py` (§15 kernel
change #1), modelled directly on `SecurityEventsMsg` — the Phase 2 precedent for
an agent-originated, non-job-bound message.

```jsonc
{
  "v": 1,
  "kind": "health_report",
  "policy_version": 7,
  "reports": [
    // a breach edge
    { "check_id": "<uuid>", "selector": "C:", "kind": "breach",
      "state": "breaching", "value": "3.1", "observed_at": "2026-08-02T09:14:00Z" },
    // a recovery edge
    { "check_id": "<uuid>", "selector": "Spooler", "kind": "recovery",
      "state": "ok", "value": "running", "observed_at": "..." },
    // a summary line
    { "check_id": "<uuid>", "selector": "", "kind": "summary",
      "state": "ok", "value": "41.0", "observed_at": "..." }
  ]
}
```

Pinned exactly: `reports` is `min_length=1, max_length=64` (a 32-check cap means
a full summary fits in one message); `value` is a **string** `max_length=64`
(§2.4's reasoning); `state` is `Literal["ok","breaching","unavailable"]`; `kind`
is `Literal["breach","recovery","summary"]`. `extra="forbid"` throughout, per
`_Msg`.

**What is NOT on this wire, ever:** raw sample arrays, raw event-log message
text, process command lines, file paths beyond a mount point, or anything
resembling a metric series. If a future field would let a consumer reconstruct a
time series, it does not belong here — that is §0.12 restated as a wire rule.

**Identity comes from the certificate-bound connection, never from the message.**
`handle_agent_message` already resolves `device_id`/`org_id` from the mTLS
binding and audits mismatches (`gateway.identity_mismatch`); the health branch
reuses that path verbatim and passes the `Device` row down, exactly as
`_handle_security_events` does. No field in this payload names a device.

Delivery is **at-least-once** (the agent's bounded offline buffer flushes on
reconnect). Idempotency is §0.10's two unique indexes; no `event_uid` is needed
because, unlike a security event, a re-delivered health edge is not a distinct
fact.

Exported to `docs/kernel/wire-protocol/HealthReportMsg.schema.json` by
`scripts/export_wire_schema.py` — the contract the agent builds its Go structs
against. Sharing a repository with the agent does not demote it to an internal
struct: see `docs/kernel/wire-protocol/README.md` for the additive-or-versioned
rule that a mixed-version fleet imposes on this shape.

### 5.2 Server → agent: `health.policy.apply` (a signed job)

```python
class HealthCheckSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    check_id: str
    check_type: str
    selector: str = ""
    comparator: Literal["gt","gte","lt","lte","eq","neq","is"]
    threshold: str          # string for the same reason `value` is
    for_ticks: int = Field(ge=1, le=60)
    cadence: Literal["tick","posture"]

class HealthPolicyApplyParams(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy_version: int
    tick_interval_s: int = Field(ge=60, le=3600)
    posture_interval_s: int = Field(ge=900)
    summary_interval_s: int = Field(ge=300, le=86400)
    checks: list[HealthCheckSpec] = Field(max_length=32)
```

Registered via `app/jobs/registry.py::register(JobType("health.policy.apply",
HealthPolicyApplyParams))`. No `result_handler` is needed for correctness — the
agent's acknowledged `policy_version` arrives in the next summary — but one is
registered anyway to stamp `device_health_policy_ack` (**§2.11**) promptly, so the
reconciliation sweep does not re-push for a whole summary interval. The handler
UPSERTs one row per device and **never lowers** a stored `policy_version`, so a
result that arrives after the next summary is a no-op rather than a regression.

An empty `checks` list is a legal policy and means "run nothing": that is how a
device whose profile was unassigned is *disarmed*, rather than left running the
last policy it was given forever. **A disarm must be an explicit instruction,
not the absence of one.**

**Amendment — the push has THREE outcomes, and `device_health_policy_ack` is
what distinguishes the second from the third.** "Push `checks=[]` when a device
is unassigned" and "push nothing when a device has no profile" are both required
and cannot both hold for the same input, because §3 answers both cases with
`None`. `push_policy` therefore branches on §2.11's ack row:

1. a profile resolves → push its checks. The list may legitimately be **empty**
   (every check filtered out by OS — a Linux device under a Windows-only
   profile), and that is still a policy, not a reason to skip;
2. nothing resolves **and the device has an ack row that is not already a
   disarm** → push `checks=[]` at `policy_version = 0` with `profile_id = NULL`.
   This is the disarm the paragraph above requires;
3. nothing resolves **and the device has no ack row** (or is already disarmed) →
   create no job. There is nothing to withdraw, and pushing anyway would be
   §0.4's arithmetic reappearing as a job per device per sweep.

**Consequently the "never lower" rule on the ack is scoped to ONE profile.** A
lower `policy_version` for the *same* `profile_id` is a late result and loses; a
*different* `profile_id` — including `NULL` — is a different policy rather than
an older copy of the same one and always wins. Without that scoping, a device
disarmed after running a version-9 profile reads forever as still on version 9,
and §12's reconciliation never re-pushes it.

**KNOWN LIMIT, as built and reproduced against a real database: deleting an
assignment does not always disarm a device.** Outcome 3 above is implemented in
`sweep.run_policy_reconcile` as `if resolved is None and acknowledged is None:
continue` — it reads §2.11's **ack** row, not the fact that a policy was
**sent**. Those two differ for exactly one window: a device that has been pushed
a policy and has not yet acknowledged it. Delete the assignment inside that
window and the reconcile sees no resolved profile and no ack, concludes "never
armed, nothing to withdraw", and creates no disarm job — while the agent, which
did receive and install the policy, keeps evaluating it. Convergence is not lost
but it is *deferred*: the device acknowledges (via the `health.policy.apply`
result handler or its next summary), an ack row appears, and the next reconcile
pass — up to `POLICY_RECONCILE_INTERVAL_S` = 900 s later — then takes outcome 2
and disarms it. A live smoke test reproduces the gap: `DELETE
/health/assignments/{id}` on a device with a sent-but-unacknowledged policy
produced **zero** `health.policy.apply` jobs, and so did an immediately following
`run_policy_reconcile`.

Stated rather than fixed, because the fix is a judgement this spec should make
explicitly and not by accident. Widening the predicate to "was a policy ever
*sent*" means reading `jobs`, which reintroduces §0.4's arithmetic on every
reconcile pass for every unarmed device in the fleet; recording a "sent" stamp
means a second column beside the ack whose only purpose is a window measured in
minutes. The behaviour is safe in the direction that matters — a device runs a
policy an operator has since deleted, it never runs one they never approved —
and §0.11's caps bound what that stale policy can be. It is recorded here so a
reader who observes "I unassigned the profile and the device kept alerting"
finds the answer at the section that promised the disarm, rather than concluding
the disarm is broken.

### 5.3 A second job type: `health.check.now`

`HealthCheckNowParams` = `{}`. Dispatching it tells the agent to sample every
check immediately and emit a full summary. Exactly the shape of `logs.ship`
(`app/jobs/registry.py`: "dispatching tells the agent to flush its buffer now;
the scheduled cadence comes from policy"). Used by the console's "refresh now"
button and after a remediation completes, so an operator does not wait a tick
interval to learn whether the fix worked.

---

## 6. Ingestion and the alert lifecycle

`handle_agent_message` gains a `HealthReportMsg` branch (§15) that resolves the
`Device` from the certificate binding and calls
`app.modules.health.ingest.ingest_report(session, device, msg)`. It follows
`_handle_security_events` exactly: no per-report audit record (volume), one
audit record for a **rejected** batch (`health.ingest_rejected`), and a UI event
queued through `queue_ui_event` so the console refreshes only after commit.

Per report line, in one transaction:

1. Resolve `(check_id, selector)` against the device's effective profile.
   Unknown, retired or removed → discard, increment an **in-memory** counter,
   continue. **Never raise**: one stale line must not reject a whole batch that
   also carries a recovery. `ingest_report` returns `(applied, discarded)`; per
   §0.8's amendment the counts ride the post-commit UI event and a non-zero
   `discarded` logs at WARNING. **There is no `health_ingest_stats` table.**
2. UPSERT `device_health_check_state` (§2.6).
3. `kind='breach'` → evaluate §0.8's OPEN predicate; on success write the alert
   plus its `health_alert_action` rows and publish `health.alert_raised`.
4. `kind='recovery'` → evaluate §0.8's CLEAR predicate clause 1.
5. `kind='summary'` with `state='ok'` → clause 2. With `state='unavailable'` →
   clause 3, if the alert's `last_seen_at` is further back than
   `unavailable_clear_after_s`.

A line whose `state` contradicts its own `kind` — a `breach` reporting `ok`, a
`recovery` reporting `breaching` — takes **no** step 3 and **no** step 4. `kind`
and `state` are independent enums on the wire with no cross-validation between
them (§5.1), so such a line parses cleanly and arrives here; step 2 still runs,
because the observation is usable even when the edge it claims is not. This is
distinct from §0.7's third state: an `unavailable` report is legitimate and has
its own correct handling (it opens nothing, clears nothing, and reaches clause 3
only as an aged `summary`), and must not be logged as a protocol violation.
Without the cross-check, `recovery`+`breaching` cleared a live alert while the
check's stored state still read `breaching`, so the next edge re-opened it and
the pair oscillated at the tick interval.

Clauses 4, 5 and 6 of the CLEAR predicate are driven by the profile-write and
assignment-write paths, the device-status path and the API respectively, not by
ingestion. Clause 1 for the one **server-evaluated** check (`agent.offline`,
§0.6) is driven by the offline sweep itself, and cannot be driven by anything
else: a server-evaluated check is absent from `agent_checks`, so every report
line naming it is discarded at step 1 and none of ingestion's clauses can ever
reach its alert.

**Escalation.** The sweep publishes `health.alert_escalated` once for an alert
that is `active` (never `acknowledged`, never `muted`), un-cleared, older than
`escalate_after_s`, and **whose check declares a `notify` action** (§7.3's
amendment — a silent-by-design check is skipped entirely and `escalated_at` is
not stamped). Once only, stamped by `escalated_at` (§2.4) —
`docs/modules/sla/spec.md` §2.5's `escalated_at` pattern, for the same reason:
a sweep that re-fires on every pass turns one problem into an hourly mail.

---

## 7. The three actions

### 7.1 `remediate`

Executed first (§0.10). The sweep, for a `pending` remediate row:

1. Load the `approved_action`; refuse if inactive, if its `job_type` is no
   longer registered, or if it is not `personas ∋ "health"`. **Amendment:** for
   `job_type='script.run'`, also refuse when `approved_action.script_version`
   does not equal the live `scripts.version` — the script was edited since it was
   approved, and §2.10 records why that comparison is the only form pinning can
   take here (`update_script` bumps the version **in place** and the approved body
   no longer exists). `last_error` names both versions; the row is not retried,
   because the fix is an admin re-approving the new version, not a delay.
2. Evaluate the fuses against `jobs` (§2.9): `cooldown_s` since the last
   dispatch of this action to **this device**; `max_per_device_per_day`;
   `max_per_org_per_hour`. Any breach → state `blocked_rate_limit` with the
   reason in `last_error`. **Blocked, not queued**: a remediation deferred an
   hour is a remediation applied to a situation that has changed. **Amendment,
   required in v1** (§2.9): `last_error` names the fuse that tripped, its window,
   and the **job ids** counted against it — because the fuse counts `jobs` by
   `type`, so what consumed the budget may be another approved action sharing
   `script.run` or a technician's manual run, and a bare "rate limited" leaves an
   operator with no way to tell which.
3. Apply the autonomy dial (§8.3).
4. Dispatch: `JobEngine.create(session, org_id=..., type=action.job_type,
   target_device_id=alert.device_id, params=<rendered template>,
   requested_by=<approver or None>)`, then publish the envelope **after the
   transaction commits** — the `deploy/scripts.py` contract.

`target_device_id` is a single `UUID`. There is no target union and no group
path in this module's dispatcher signature; §8.4 makes that a structural claim
rather than a convention.

The job's outcome flows back through the ordinary `job_result` path and the
kernel's `jobs.succeeded`/`jobs.failed` audit records. A **failed** remediation
does **not** clear the alert and does **not** re-fire (`UNIQUE (alert_id,
kind)`); it posts a comment on the ticket if one exists and leaves the alert
open for a human. Auto-remediation that retries itself is how a service-restart
loop becomes an outage.

### 7.2 `open_ticket`

`tickets.create_ticket(session, org_id, actor_id=None,
TicketCreate(type="incident", subject=..., description=..., device_id=...,
category_id=settings.default_ticket_category_id,
queue_id=settings.default_alert_queue_id), origin="alert",
source_alert_id=alert.id)`.

Verified against the code: `create_ticket` already enforces
`origin="alert" ⇒ type="incident"`, already forces `requester_id`/`created_by`
to `NULL` for alert origin regardless of what the caller passes, and already
refuses `source_alert_id` for any other origin. `ck_ticket_human_attribution`
backs it at the database layer. Nothing here needs building; see §15.

The description carries: hostname, check type, selector, comparator, threshold,
observed value, opened-at, the remediation action and job id if one was
dispatched, and — when `previous_alert_id` is set — the previous alert's ticket
number. It carries **no** raw log text and **no** script body.

### 7.3 `notify`

Publishes nothing itself: the alert's `health.alert_raised` event is already in
the outbox from ingestion. The `notify` action row exists so that a check
*without* it can be silent-by-design (console-only, e.g. an `info`-severity
posture drift), which a shared org-wide notification rule could not express.
The row is marked `done` when the outbox event for that alert exists.

**Amendment — silence is the absence of an event, not a flag on one.** An earlier
draft published the event with a `"notify": false` marker "and the org's rule set
is expected to scope on it". **It cannot.** Verified in
`app/modules/notification/routing.py::resolve_recipients`: rule selection is
`NotificationRule.org_id == event.org_id AND event_type == … AND active IS TRUE`,
narrowed only by `queue_id` (matching or NULL). Nothing in rule selection, and
nothing in `_expand_rule`, reads any other payload key. `notification_rules` has
no column that could express it. A check marked "silent by design" therefore
notified exactly as loudly as every other check — the marker was decoration, and
worse than decoration, because it read as a working guarantee.

**Decision: when a check has no `notify` action, the module does not publish the
event at all.**

- `health.alert_raised` and `health.alert_cleared` are **not published** for an
  alert whose check declares no `notify` action.
- `health.alert_escalated` is not published either, and §12's escalation sweep
  **skips** such alerts without stamping `escalated_at`. An alert not worth a mail
  when it opened does not become worth one by ageing; escalating a deliberately
  console-only posture drift after an hour is the flag's failure arriving late.
- `health.remediation_requires_approval` **is always published**, silent check or
  not. It is not an alert notification — it is a request addressed to a named
  human who must act, and §9's own argument applies: an approval request nobody
  is told about expires at `approval_ttl_s` and the dial's middle setting
  silently means "never".
- The alert row, its `health_alert_action` rows, the audit records and the UI
  event are written exactly as for any other alert. **"Silent" means unrouted,
  never uncounted** — §0.9's rule that suppression removes actions and never
  evidence, applied to notification.
- `"notify"` is consequently **removed from the §9.1 payload**. A field no
  consumer can act on is a field a future reader will assume works.

Rejected: **a scoping column on `notification_rules`.** A notification-module
schema change, a new dimension on every rule in the org, and a question every
future publisher would then have to answer — all to express a boolean this module
already knows before it publishes.

**The cost, stated:** an org cannot re-enable notification for a silent check
from the notification side; it is a health-module edit (add a `notify` action to
the check), by someone holding `health.admin`. That is the correct owner and the
wrong-feeling place to look, and it will generate a support question. Accepted,
because the alternative is a routing surface where two subsystems both believe
they decide whether a mail is sent.

**How this is tested (§14):** the observable is a **delivered-recipient count**,
never the presence of a flag. With an org-wide rule for `health.alert_raised`
resolving to at least one recipient, an alert from a check *with* a `notify`
action yields a `domain_events` row and `len(resolve_recipients(...)) >= 1`; an
alert from a check *without* one yields **zero `domain_events` rows** for that
alert and therefore zero recipients. A test asserting `payload["notify"] is
False` would have passed against the defect this amendment fixes, and is
forbidden.

### 7.4 Ticket auto-resolution on recovery

When an alert clears and `settings.auto_resolve_alert_tickets` is true and the
alert has a `ticket_id`:

- If the ticket has **not been touched by a human** — no `user`-actor
  `ticket_status_history` row, no `user`-authored comment, no assignee — drive it
  to **`resolved`** via `transitions.change_status(..., to_status="resolved",
  actor_type="system", actor_id=None)` and post a system comment naming the
  recovery and its `cleared_reason`.
- If it **has** been touched, post the system comment and **change nothing
  else**.

**Recommended and justified: `resolved`, never `closed`.** This adopts
`docs/modules/service-desk/spec.md` §7.2's pinned contract verbatim, and the
reason is worth restating: closing removes the ticket from every "needs a human
look" view, and the interesting case — a disk that filled, was cleaned by
remediation, and will fill again in six hours — is exactly the one where the
recovery is the *least* informative event. `resolved` keeps a human in the loop
for one more beat, and the SLA engine's existing auto-close
(`sla_org_settings.auto_close_after_days`) then closes it on the org's own
schedule. That mechanism already exists and this module adds nothing to it.

**Why the human-touched exception:** a technician mid-investigation whose ticket
resolves itself under them loses it from their queue, and the recovery may be
*their* doing or may be the symptom vanishing while the cause remains.

**KNOWN LIMIT: the split this section mandates is not computable as a report
from the APIs that exist.** The definition above is on ticket data —
`ticket.origin='alert'` joined to `ticket_status_history.actor_type='system'` —
and two things stand between that definition and a number:

1. **The ticket list cannot select the population.**
   `GET /service-desk/tickets` filters on `type`, `status`, `queue_id`,
   `assigned_to`, `requester_id`, `device_id`, `priority` and a text `q`. There
   is **no `origin` filter**, so "every `origin='alert'` ticket in this period"
   is not a query any caller can express.
2. **Status history is per ticket and only per ticket.**
   `GET /service-desk/tickets/{id}/history` **does exist** and does return
   `actor_type` — so the raw fact is reachable — but only one ticket at a time,
   with no aggregate endpoint. Computing the split therefore means one request
   per alert ticket over a set point 1 says you cannot enumerate.

*(This bullet corrects an earlier claim, made in `HealthReportPanel.tsx`'s
rendered caveat and its comment, that "no route exposes that history". That is
false — `list_status_history_route` has been routed since the ticket core's Task
13 fix round. The frontend copy is wrong about the reason and right about the
conclusion, and the copy should be fixed to say "no aggregate, and no `origin`
filter" instead.)*

The console therefore computes a *different* number: it splits on the **alert's**
own `cleared_reason` (`recovery`/`summary_ok` = the condition went away;
`manual` = a person closed it), which answers "what closed the alert", not "who
closed its ticket". The two disagree in both directions — an alert that clears
by recovery whose ticket a technician had already resolved counts as
machine-resolved, and a ticket a technician resolves before the condition clears
is invisible until the alert clears at all.

`HealthReportPanel.tsx` renders the caveat **on the panel**, next to the
numbers, rather than only in a comment. That is the mitigation, and it is
deliberately weaker than the rule: a number whose definition differs from this
spec's and does not say so is worse than no number, but it is still not the
number §7.4 asks for. Closing it is service-desk work — an `origin` filter on
the ticket list, or a reporting aggregate over `origin='alert'` — not a change
here. This section's definition stands; the frontend's approximation is the
thing that should be replaced.

**The perverse incentive, accepted and stated:** untouched alert tickets
auto-resolve, which manufactures an excellent MTTR figure for work nobody did.
Mitigation is attribution, not a softer rule — `ticket.origin='alert'` plus
`ticket_status_history.actor_type='system'` make machine-resolved and
human-resolved separable, and **reporting is required to separate them** (§13).
A dashboard that adds them together is a defect in the report, not in this rule.

**A re-opened condition opens a NEW alert and a NEW ticket.** Rejected:
reopening the cleared alert's ticket. The reopen edges exist in the ticket core,
but a ticket that reopens six times in a month is unreadable as an incident
record and destroys per-incident MTTR. The cost is ticket volume for a chronic
flapper; §0.9's suppression is what bounds it, and `previous_alert_id` is what
makes the history legible.

---

## 8. Remediation governance

### 8.1 It is always a signed, audited job

Invariant #1, with no exception path. There is no "run this command on the
device" API in this module, no shell string in any params model, and no code
path that reaches an endpoint other than `JobEngine.create()` →
`publish_job()`. The agent verifies signature, target identity, freshness and
nonce before any capability runs (`docs/05-agent-spec.md`), and it does so for
a remediation exactly as for an operator-initiated job, because it is the same
envelope.

### 8.2 The registry shape

§2.9. Three properties matter most, and each closes a specific hole:

- **`job_type` validated against the live registry at write time.** An approved
  action naming a job type that does not exist is a configuration bug that must
  surface when someone writes it, not at 03:00 when an alert fires.
- **`script_id` + `script_version` pinned.** Without the version, approving a
  script approves its future contents.
- **`params_template` is frozen, with a closed substitution set.** Exactly two
  substitutions are permitted — `{device_id}` and `{selector}` — validated
  against a per-job-type allowlist of which keys may receive them. `{selector}`
  carries the alert's selector (a service name, a mount point), which is already
  bounded by `health_check`'s own validation. This is **not** a template
  language, and adding one would re-open §0.1 from the server side.

**Amendment — `SUBSTITUTABLE_PARAM_KEYS`, enumerated.** Named but never listed in
the earlier draft. Enumerated here against the job types that actually exist,
read from their params models rather than from the brief:

```python
# app/core/approved_actions.py — kernel-owned.
# Which params keys may receive a substitution, per job type. A job type absent
# from this map permits none; this is NOT a job-type allowlist (that is
# `registered_names()`, per §2.9). Adding a key is a reviewed code change.
SUBSTITUTABLE_PARAM_KEYS: dict[str, frozenset[str]] = {
    "script.run":        frozenset(),
    "app.deploy":        frozenset(),
    "patch.apply":       frozenset(),
    "patch.scan":        frozenset(),
    "inventory.collect": frozenset(),
    "logs.ship":         frozenset(),
    "backup.run":        frozenset(),
    "backup.restore":    frozenset(),
    "provision.enroll":  frozenset(),
    # `hello` and `remote.session.open` are absent deliberately: the first is a
    # kernel smoke type, the second needs a relay URL and a relay token this
    # module cannot mint. Absent and empty behave identically; the difference is
    # that an empty entry is a reviewed statement and an absent one is silence.
}
```

**Every entry is empty in v1, and that is a finding about the code, not an
omission.** No registered job type has a params key a device id or a selector may
be written into. Verified model by model:

- **`ScriptRunParams`** (`app/modules/deploy/schema.py`) is `interpreter`, `body`,
  `script_id`, `script_version`, `timeout_s`. **There is no arguments field** — a
  script on this platform takes no parameters; the body is the entire input.
  Four of those five are built by the dispatcher from the pinned `scripts` row
  (§2.10), and `timeout_s` is a number. So "restart the Spooler" is a script whose
  body names the Spooler, and per-service remediation is **one approved action per
  service**.
- **`PatchApplyParams`** is `patches`, `reboot`, `deferral_hours`; **`reboot` is
  `Literal["never","if_required","forced"]`, not a boolean** (§8.4's example is
  corrected below). None of the three takes a device or a selector.
- **`AppDeployParams`**, **`BackupRunParams`**, **`BackupRestoreParams`**,
  **`ProvisionEnrollParams`**: every field is an artefact reference, a repository
  reference, a detection rule or a credential-shaped token. An operator-supplied
  selector may set none of them.
- **`InventoryCollectParams`**, **`LogsShipParams`**, **`PatchScanParams`**,
  **`HelloParams`**: empty or a single default string.

**A wrong entry here cannot reach an endpoint anyway**, which is the reason the
map is a *narrowing* control rather than the only one: every params model is
`extra="forbid"` and `JobEngine.create` validates the rendered params against the
registered model before signing (`app/jobs/engine.py`), so a substitution into an
unknown key raises `ValueError` at create time.

**`render_params_template` walks the structure; it must never call
`str.format`.** The renderer recurses through dicts and lists, and substitutes
only where a string is *exactly* `"{device_id}"` or `"{selector}"` **and** its key
is in that job type's frozenset. Any other `{` or `}` in any string in the
template raises. `str.format` over the template is forbidden for three separate
reasons, any one of which is sufficient:

1. **It is an object-graph read.** `format` resolves attribute and index access,
   so `"{0.__class__.__init__.__globals__}"` in a customer-authored template
   walks whatever object is passed in. A template written by an org admin is
   untrusted input to a process holding the job signing key.
2. **It fails at the wrong time.** An unknown name raises `KeyError` at dispatch —
   03:00, with an alert already open — where a structural walk raises at *write*
   time and the API returns 400, which is §8.2's whole posture.
3. **A legitimate selector contains braces.** A Windows volume path is
   `\\?\Volume{GUID}\`, and `disk.free_pct`/`disk.free_bytes` take a volume as
   their selector (§4.1). Under `format` that selector is a formatter exception;
   under the walk it is data, because the selector is a *substituted value* and is
   never itself re-scanned for tokens.

**The cost, stated:** `{device_id}` and `{selector}` exist in the design with no
destination in v1. The mechanism is built, tested and unused — deliberately, so
that the first job type that needs it does not also have to invent it. The
perverse incentive this accepts is real and should be named: the obvious way to
make one approved action cover every service is to add a free `args: str` to
`ScriptRunParams`. That is an unbounded, operator-controlled string in a params
model that executes as SYSTEM/root — §0.1's free-form expression arriving from the
server side instead of the edge. If a later phase wants it, it is a `deploy`
module change with its own review and its own threat model, never a health-module
convenience.

### 8.3 The autonomy dial

| value | behaviour |
|---|---|
| `suggest` | Nothing is dispatched. The alert carries the suggested action; the console offers one-click dispatch by a user holding `health.remediate`. |
| `require_approval` | The action row goes `awaiting_approval`, `health.remediation_requires_approval` is published, and a user holding `health.remediate` approves or refuses. Unapproved after `approval_ttl_s` → `expired`. |
| `autonomous` | The sweep dispatches with no human, subject to §7.1's fuses. |

**`autonomous` is refused at write time** — API validation *and* the CHECK
constraint in §2.9 — whenever the action is `destructive`, `risk_class='high'`,
or `max_target_scope <> 'device'`. That is the brief's "nothing destructive or
fleet-wide auto-fires" as a database constraint. A future persona cannot relax
it in application code; it takes a migration, which a reviewer sees.

All v1 seeded actions ship at `require_approval` except `collect-inventory`.
Autonomy is opted into deliberately, per action, by a named admin, and the
change is audited (§11).

### 8.4 "Destructive" and "fleet-wide", in code terms

**Destructive** is *derived and stored*, never accepted from a request body. One
pure function, `app/core/approved_actions.py::is_destructive(job_type,
params_template, script_exemption) -> bool`, so this module and Phase 17 cannot
disagree. It returns true iff any of:

1. `job_type ∈ _DESTRUCTIVE_JOB_TYPES = {"backup.restore", "provision.enroll",
   "app.deploy", "patch.apply"}` — each either overwrites data, re-images a
   machine, or changes installed software without a human decision. (Checked
   against the live registry: `hello`, `inventory.collect`, `logs.ship`,
   `patch.scan`, `patch.apply`, `app.deploy`, `script.run`, `backup.run`,
   `backup.restore`, `provision.enroll`, `remote.session.open`.)
2. `job_type == "script.run"` **and** no exemption exists for its exact
   `(script_id, script_version)` pair. **Arbitrary code is
   destructive-by-default**: the server cannot know what a PowerShell body does,
   and pattern-matching script bodies for dangerous verbs is security theatre
   that fails on the first base64 blob. An exemption is granted per exact
   version, by a user holding `health.action.admin`, and is audited
   (`health.action.destructive_exempted`). Editing the script mints a new
   version and the exemption does not follow it. **The exemption is a row in
   `approved_action_script_exemption` (§2.10)**, which is where that table is
   specified and why it is keyed to the pair rather than to an approved action;
   `is_destructive` stays pure and takes the row (or `None`), it does not query.

   > **Amendment — the signature, as built.** The stated three-parameter
   > signature cannot decide this clause. The exemption is keyed to a
   > `(script_id, script_version)` **pair**, and that pair lives on the
   > `approved_action` row (§2.9) — *not* in `params_template`, because §8.2 has
   > the dispatcher build `ScriptRunParams` from the pinned `scripts` row, and
   > duplicating the pin into the template would create two truths that can
   > disagree. Given only the exemption row, the function would have to trust
   > whatever the caller looked up, which is exactly what "granted per exact
   > version" forbids. **As built** the pin is two *keyword-only* parameters,
   > leaving the three positional ones exactly as named above:
   > `is_destructive(job_type, params_template, script_exemption, *,
   > script_id=None, script_version=None)`. It **fails closed**: a `script.run`
   > action with no pin matches no exemption and is destructive (the
   > `ck_approved_action_script_pin` CHECK makes such a row unrepresentable
   > anyway). The pair→row lookup is
   > `app/core/approved_actions.py::get_script_exemption`, which carries its own
   > explicit `org_id` predicate in addition to RLS.
   >
   > **Amendment — where the declarative models live.** §2.9 says the table
   > "lives in `backend/app/core/approved_actions.py` with its model beside the
   > other kernel models". In this codebase those two halves contradict each
   > other: the other kernel models are in `app/models/` (`core.py`,
   > `security.py`, `vault.py`), and a declarative model under `app/core/`
   > cannot be registered in `app/models/__init__.py` without a real import
   > cycle — `app/core/approved_actions.py` needs `app.models.base.Base`, and
   > importing that executes `app/models/__init__.py`, which would be part-way
   > through importing `app.core.approved_actions`. Verified, not assumed:
   > `ImportError: cannot import name 'ApprovedAction' from partially
   > initialized module`. **As built**, `ApprovedAction` and
   > `ApprovedActionScriptExemption` are declared in
   > `app/models/approved_actions.py` — beside the other kernel models, as the
   > second half of §2.9 says — and re-exported from
   > `app/core/approved_actions.py`, which remains the single module that owns
   > the concept and the only one a caller imports it from. This also keeps the
   > cycle from reappearing when the health module's own models land.
3. `params_template` sets a key listed in the per-job-type
   `DESTRUCTIVE_PARAM_KEYS` map — e.g. `patch.apply` with a `reboot` other than
   `"never"`. **Amendment:** an earlier draft wrote this as `reboot: true`;
   `PatchApplyParams.reboot` is `Literal["never","if_required","forced"]`, not a
   boolean (`app/modules/patch/schema.py`). `DESTRUCTIVE_PARAM_KEYS` is therefore
   a map of key → the set of values that make it destructive, not a set of key
   names — `{"patch.apply": {"reboot": {"if_required", "forced"}}}` — because a
   key-name-only map would mark every `patch.apply` action destructive including
   `reboot: "never"`, and an over-broad gate is one that gets argued away.

**Fleet-wide.** An action is fleet-wide iff a single logical invocation would
produce jobs for more than one device. In this module that is **structurally
impossible**, by three independent mechanisms:

1. the dispatcher's signature takes `device_id: UUID`. There is no target union
   to pass a group through. `deploy.scripts.run_script`'s `target_type="group"`
   path exists but is unreachable from here — this module does not call it;
2. `approved_action.max_target_scope` is CHECK-constrained to `'device'` in v1;
3. `max_per_org_per_hour` (default 20).

Mechanism 3 is the one that matters in practice, and it addresses the *real*
fleet-wide risk, which is not a group-targeted job. It is **4,000 individual
device-scoped jobs fired in the same minute** because an NTP jump, a DNS
outage, or a bad monitoring assumption tripped one check across the whole
estate at once. Without an org-level fuse, "device-scoped only" is a comforting
label on a fleet-wide event. With it, the outcome is 20 remediations and 3,980
alerts in `blocked_rate_limit` — visible, bounded, and reviewable.

### 8.5 What Phase 17 inherits

The registry is defined here so the AI layer joins it rather than forks it:
`personas` (add values to existing rows), `risk_class` (already present),
`autonomy` (one dial), `destructive` (one derivation function), and the
`autonomous` CHECK (one gate). `docs/modules/ai-admin.md` §"Registry model
detail" asks for exactly the persona + risk-class dimensions on **one**
registry; this is that registry. Phase 17 adds `ai_session`/`ai_action` and its
orchestration service; it does not add a second approved-action table, and a
review that sees one should reject it.

---

## 9. Events, and the obligations registering one incurs

Four new types in `app/core/events.py::EVENT_TYPES`:

| event type | published when | subject |
|---|---|---|
| `health.alert_raised` | an alert opens (§0.8) **and its check declares a `notify` action** (§7.3) | `health_alert` |
| `health.alert_cleared` | an alert clears (§0.8), same condition | `health_alert` |
| `health.alert_escalated` | escalation sweep, once per alert, same condition | `health_alert` |
| `health.remediation_requires_approval` | a `require_approval` action row enters `awaiting_approval` — **always, silent check or not** (§7.3) | `health_alert` |

Four rather than two: escalation reusing `alert_raised` would double-fire every
rule routed to the on-call channel, and an approval request with no event type
means the dial's middle setting silently means "never". This is the exact
mistake `app/core/events.py` already documents and already fixed for
`ticket.approval_requested` vs `ticket.approval_decided`; repeating it would be
choosing a known defect.

**Registering an event type is an enforced four-part contract.** Each of the
four types above must ship, in the same change:

1. an entry in `EVENT_TYPES` (`app/core/events.py`) with a comment naming the
   publishing module — `publish()` raises `UnknownEventType` otherwise;
2. an **email renderer** in `_EMAIL_RENDERERS`
   (`app/modules/notification/delivery.py`).
   `test_every_registered_event_type_has_an_email_renderer` asserts
   `set(_EMAIL_RENDERERS) == EVENT_TYPES` — a missing renderer fails the suite
   rather than silently falling through to `_render_generic`'s machine notice;
3. a **pinned wire-format sample** in `_EVENT_TYPE_SAMPLE_PAYLOADS`
   (`backend/tests/test_notification_delivery.py`).
   `test_every_registered_event_type_has_a_pinned_wire_format_sample` asserts
   set equality there too, and the parametrised webhook test replays it against
   the real delivery path;
4. an entry in `STAFF_ONLY_EVENT_TYPES` **plus a matching DB CHECK on
   `notification_rules`** in the migration — see below.

### 9.1 The payload contract

`app/modules/notification/routing.py` fixes the payload shape: every id is a
`str(uuid)` or `None`, and `queue_id`, `assignee_id`, `requester_id` are read
for every event.

```jsonc
{
  "alert_id": "…", "device_id": "…", "hostname": "web-01",
  "check_type": "disk.free_pct", "selector": "C:",
  "severity": "critical", "state": "active",
  "observed_value": "3.1", "threshold": "10", "comparator": "lt",
  "opened_at": "…", "cleared_at": null, "cleared_reason": null,
  "flapping": false,
  "ticket_id": null, "ticket_number": null,
  "remediation_job_id": null, "remediation_action_key": null,
  "queue_id": "…", "assignee_id": null, "requester_id": null
}
```

**`queue_id` is present and non-NULL, and this is a discovered constraint, not a
decoration.** Verified in `routing.resolve_recipients`: when an event's payload
carries no `queue_id`, rule selection is narrowed to `queue_id IS NULL` rules
**and** `recipient_kind="queue_members"` expands against a NULL queue, returning
`[]`. The only recipient kind that resolves to anything for a queue-less event
is therefore `channel_default` — i.e. one webhook or one from-address for the
entire org. That is not an on-call rota. So health events carry
`queue_id` = the alert ticket's queue if one was opened, else
`health_org_settings.default_alert_queue_id`. The alternative — accepting
`channel_default`-only routing — was considered and rejected: alert routing that
cannot address a team is alert routing nobody will use.

`assignee_id` and `requester_id` are present-and-`null` so the "every id present"
convention holds and a future reader does not have to distinguish "absent"
from "null".

**`"notify"` is absent, by amendment (§7.3).** An earlier draft carried
`"notify": true|false` and expected notification rules to scope on it;
`routing.resolve_recipients` cannot read a payload key, so the flag decided
nothing. Silence is now the absence of the event, which means the payload never
needs to describe it. Do not re-add the key: a field no consumer can act on is a
field a future reader will assume works.

**Never in a payload:** raw event-log text, script bodies, credentials, full
summary blobs, or a device's file paths beyond a mount point. The outbox
docstring's rule — "only publishable content, never a raw model dump" — applies
here with the extra weight that a webhook destination is off-platform.

### 9.2 Staff-only, and a correction to a kernel comment

All four types are added to `STAFF_ONLY_EVENT_TYPES` with matching per-type
CHECK constraints on `notification_rules` (the `ck_rule_no_internal_to_requester`
/ `ck_rule_no_sla_breach_to_requester` pattern, migrations 0025 and 0034).

An alert has no requester, so the pairing is not a live hole today. The guard is
added anyway because it costs one CHECK per type and because the structural
argument for skipping it is **not sound as written in the kernel**:

> `app/core/events.py` lines 79–83 state that `recipient_kind="requester"`
> "resolves the recipient from the TICKET, not from the event payload, so
> omitting `requester_id` from the SLA payload does NOT make the pairing
> unroutable". **`routing.py` does not do that.** `_expand_rule` resolves
> `requester` from `event.payload["requester_id"]`
> (`_uuid_or_none(payload.get("requester_id"))` → `_resolve_single_user`), so an
> absent `requester_id` genuinely does resolve to `[]`.
> `delivery.py::_render_ticket_sla_breached`'s docstring says the same thing
> `routing.py` does; the `events.py` comment is the outlier. The guard's
> **conclusion** is right and the constraint should stay; only its stated
> mechanism is wrong. Recorded here rather than silently relied upon — this spec
> does not depend on either reading, because it adds the guard regardless.

---

## 10. API

Under `/api/v1/health`, versioned per convention, literal segments before
`{id}`.

| Method | Path | Permission |
|---|---|---|
| GET/POST | `/health/profiles` | `health.read` / `health.admin` |
| GET/PATCH/DELETE | `/health/profiles/{id}` | read / admin / admin |
| POST | `/health/profiles/{id}/clone` | `health.admin` |
| GET/PUT | `/health/profiles/{id}/checks` | read / admin |
| GET/POST | `/health/assignments` | read / admin |
| DELETE | `/health/assignments/{id}` | `health.admin` |
| GET | `/health/check-types` | `health.read` |
| GET | `/health/alerts` | `health.read` |
| GET | `/health/alerts/summary` | `health.read` |
| GET | `/health/alerts/{id}` | `health.read` |
| POST | `/health/alerts/{id}/acknowledge` | `health.alert.manage` |
| POST | `/health/alerts/{id}/mute` | `health.alert.manage` |
| POST | `/health/alerts/{id}/close` | `health.alert.manage` |
| POST | `/health/alerts/{id}/remediate` | `health.remediate` |
| POST | `/health/alerts/{id}/actions/{action_id}/approve` | `health.remediate` |
| POST | `/health/alerts/{id}/actions/{action_id}/refuse` | `health.remediate` |
| GET/PUT | `/health/settings` | read / admin |
| GET | `/devices/{device_id}/health` | `devices.read` |
| POST | `/devices/{device_id}/health/check-now` | `health.alert.manage` |
| GET/POST | `/approved-actions` | `health.read` / `health.action.admin` |
| GET/PATCH/DELETE | `/approved-actions/{id}` | read / action.admin / action.admin |
| POST/DELETE | `/approved-actions/{id}/exempt-script` | `health.action.admin` |

**Amendment — `check-now` moved from `health.read` to `health.alert.manage`,
and grew a fuse.** The table above originally gated
`POST /devices/{device_id}/health/check-now` on `health.read`, and `api.py`
followed it as written while recording the cost in its own docstring: "it is
still rate-limited by nothing, which is the honest cost of following the table."
The cost was then measured. A principal holding **exactly** `health.read` — the
code migration 0040 grants to `read-only` and `auditor`, the two lowest seeded
tiers — issued 30 consecutive requests and got 30 signed jobs, all `200`. There
is no fleet-wide target in this module (§8.4) and there does not need to be: a
loop over a 5,000-device fleet is a fleet-wide dispatch assembled one device at
a time, driven by an account whose role name says it may not change anything.

Two things were wrong and both are now fixed, because either alone leaves the
other open:

1. **The gate.** Minting a signed job is a write at an endpoint whatever the job
   does, and invariant #6 is about *every* job dispatch, not only the
   destructive ones. `health.alert.manage` — the module's
   operate-but-do-not-configure code, seeded to the technician tier — is the
   right audience for "sample this box now"; `health.read` keeps every route
   that only reads. `check-now` is therefore no longer the one route in the repo
   where a read-tier code dispatches a job. Migration 0040's comment describing
   `health.alert.manage` as dispatching nothing is now narrower than the
   permission; it is a shipped migration and is left untouched, and this table
   plus `app/core/rbac.py` carry the current definition.
2. **The volume.** A technician can loop too, and no gate can tell a loop from a
   click. `api.py::_check_now_fuse` bounds `health.check.now` to one job per
   device per 60s and 500 per org per hour, **counted against `jobs`** per §2.9
   — the same rule §7.1's remediation fuses follow, chosen over a Redis counter
   because the thing being limited *is* a `jobs` row and a second counter would
   be a second truth that can disagree with what actually reached a device. A
   breach is `429`, not `400`: the request is well formed and the state refuses
   it. Nothing in this module dispatches `health.check.now` automatically, so
   the console button is the only producer and these numbers only ever bind a
   script.

**Amendment — the table is 31 routes, and it was 30.** Expanded one row per
method it comes to 31, and the row that was missing is
`DELETE /approved-actions/{id}/exempt-script`. The prose four paragraphs below
has always required it ("the matching `DELETE` revokes it; both are audited"),
`api.py` implements it, and `tests/test_health_api.py::EXPECTED_ROUTES` pins it
— only this table omitted it. Recorded as an amendment rather than silently
fixed because the omission is instructive: a revoke route that exists in prose,
in code and in the RBAC test but not in the route table is exactly the shape of
route that ships ungated, and the reason that test derives completeness from the
router instead of from this table.

Checks are replaced as a set (`PUT`), mirroring
`docs/modules/sla/spec.md` §9's window handling: a profile's checks only make
sense together, and a partial edit can leave a profile that no longer matches
the `policy_version` an agent acknowledged.

`GET /devices/{device_id}/health` is gated on `devices.read`, not `health.read`,
for the same reason the SLA spec gates the per-ticket view on `ticket.read`: a
technician looking at a device needs its health without holding health
administration rights.

A profile referenced by any assignment cannot be deleted; the error names the
assignments. Deleting a profile or check leaves existing `health_alert` rows
intact — they record what was true at the time, and rewriting history because
an admin deleted a row destroys the record's value.

**Amendment — what "deleting a check" means at this API** (§2.4's amendment has
the reasoning). `PUT /health/profiles/{id}/checks` and `DELETE
/health/profiles/{id}` **retire** the checks they drop: open alerts are cleared
`policy_removed`, `retired_at` is stamped, `policy_version` is bumped and the
policy is re-pushed. The alert rows survive with a resolvable `check_id`, which is
what makes the promise above true rather than aspirational. A hard row delete
succeeds only for a check that never opened an alert; when the
`ON DELETE RESTRICT` FK fires the API returns **409 naming the blocking alerts**
(count plus the ids of the most recent few), never a raw `IntegrityError`.

**Amendment — `GET /health/alerts/summary`, because the badge's count was a
floor.** The table above listed the alert LIST and nothing that summarised it,
and the list's `state` filter is single-valued — so "open" (active OR
acknowledged OR muted, §0.8) is not a query any caller can express against it.
The console's health glyph therefore fetched one page of `GET /health/alerts`
(the route's own default, 200 rows, capped at 500) and filtered it in the
browser through `healthState.ts::isAlertOpen`. On a fleet with more than a page
of open alerts that count under-reports, silently; the shipped console was
honest about it and rendered "at least 40 needing attention", which is a floor
where an operator reads a total.

The route answers the total. It returns `open` (every non-`cleared` alert in the
org), `by_state` over the three OPEN states, `by_severity` over all three
severities with every bucket present and zero, and `worst_severity` — null only
when nothing is open, never "unknown". Gated `health.read` like its neighbours:
it counts rows the same principal may already list one by one. The counting is a
single `GROUP BY (state, severity)` in Postgres, which is the load-bearing half —
a handler that loaded the rows and counted them in Python would carry the same
bound one layer down. Registered **before** `/health/alerts/{id}`, whose
parameter is a `UUID`: the wrong order is not a shadowed route but a 422 on a
totals endpoint.

`POST /approved-actions/{id}/exempt-script` writes a row in
`approved_action_script_exemption` (§2.10) for the action's **pinned**
`(script_id, script_version)` — it does not take the pair from the request body,
so an exemption can never be granted for a version the action does not name.
Refused with 400 when the action's `job_type` is not `script.run`, and when the
action's pinned `script_version` no longer matches the live `scripts.version`
(exempting a version that has already been edited away is a no-op that reads as a
grant). The matching `DELETE` revokes it; both are audited (§11.3).

---

## 11. RBAC, tenancy, audit

### 11.1 Permissions (added to `app/core/rbac.py::PERMISSIONS`)

- **`health.read`** — view profiles, checks, assignments, alerts, device health.
- **`health.alert.manage`** — acknowledge, mute, manually close. Technician tier:
  this is the day-job.
- **`health.admin`** — create/modify/delete profiles, checks, assignments, and
  org settings. *High privilege: it decides what the entire fleet alarms on and
  can silence every alert in the organisation by deleting one assignment.*
  Admin tier.
- **`health.remediate`** — dispatch a remediation, or approve/refuse one.
  *High privilege: it puts a signed job on an endpoint that executes as
  SYSTEM/root.* Deliberately **not** bundled into `health.admin` — configuring
  what alarms and executing code on endpoints are different acts and different
  blast radii.
  **On the two routes that dispatch, it is the floor and not the whole check.**
  `remediate` and `actions/{id}/approve` additionally require the permission the
  resolved job type's own module route requires — `patch.apply` for a
  `patch.apply` remediation, `deploy.script.run` for a `script.run` one — and a
  job type with no human dispatch route is refused outright. §20 is the whole
  argument; `refuse` is exempt because it creates no job.
- **`health.action.admin`** — manage the `approved_action` registry, set the
  autonomy dial, grant a script destructive-exemption. *The highest grant in
  this module: it decides what MAY be run automatically as SYSTEM/root across
  the fleet.* Seeded to `super-admin` and `org-admin` only, **never** technician
  — the same posture `notification.admin` and `sla.admin` take, for the same
  reason.

Every route carries `require_permission` server-side (invariant #6), with RBAC
completeness derived from the router by `route_permission` — a route added
without a permission dependency fails the existing completeness test. That
derivation sees `health.remediate` on both dispatching routes and nothing else,
which is correct: §20's second permission depends on the request body and cannot
be a route dependency, so it is a service-layer check and is tested as one
(`tests/test_health_remediation_authz.py`).

### 11.2 Tenancy (invariant #5)

All **nine** module tables (§2.1–§2.8, §2.11) plus the **two** kernel tables
(`approved_action` §2.9, `approved_action_script_exemption` §2.10) carry `org_id`,
get RLS `ENABLE + FORCE` with `tenant_isolation` (`USING` **and** `WITH CHECK`),
and every query in this module carries an explicit `org_id` predicate in addition
to RLS. One behavioural cross-org test per table, each of which fails if its
predicate is removed.

The ingest path runs inside `org_session(org_id)` where `org_id` comes from the
**certificate-bound connection**, never from message content — the existing
`handle_agent_message` contract.

`approved_action` has **no global rows**: a cross-tenant approved action is a
cross-tenant command path, and a "shared library of safe remediations" is a
seeding concern, not a row-visibility concern. Seeds are copied per org.

### 11.3 Audit (invariant #4)

**Audited:**

- profile / check / assignment create, update, delete
  (`health.profile_*`, `health.check_*`, `health.assignment_*`);
- org settings change (`health.settings_updated`);
- `approved_action` create, update, delete (`health.action_*`);
- **every autonomy-dial change**, with old and new value
  (`health.action_autonomy_changed`);
- **every script destructive-exemption**, with `(script_id, script_version)`
  (`health.action.destructive_exempted`);
- every remediation dispatch (`health.remediation_dispatched`) carrying
  `alert_id`, `action_key`, `job_id`, `autonomy`, `approved_by` — **in addition
  to** the kernel's own `jobs.dispatched`, because the kernel record says a job
  was sent and this record says *why a machine decided to send it*;
- every approval and refusal (`health.remediation_approved` / `_refused`);
- acknowledge, mute, manual close (`health.alert_*`);
- alert→ticket creation (`health.alert_ticket_opened`) — the ticket core writes
  its own `ticket.created` record too; both are wanted, as they attribute
  different acts;
- a **rejected** health report batch (`health.ingest_rejected`), mirroring
  `security_logs.ingest_rejected`.

**Not audited:** individual breach/recovery/summary ingests, sweep cycles, and
policy re-pushes. Same reasoning `docs/modules/security-logs/spec.md` and
`docs/modules/sla/spec.md` §8 give: operational volume would swamp a
hash-chained log whose value is that a human can read it.

---

## 12. Worker

Five beat entries, following `app/worker.py`'s established per-org shape (thin
scheduling shell; logic in `app/modules/health/`, tested directly):

| task | interval | does |
|---|---|---|
| `health-alert-actions` | 30 s | execute `pending`/approved `health_alert_action` rows (§7), with claim-before-execute and a stuck-claim sweep on the notification module's 10-minute precedent |
| `health-alert-sweep` | 60 s | mute expiry → `active` + one re-notify; escalation (`escalate_after_s`, once); `unavailable_timeout` clears; approval expiry |
| `health-offline-sweep` | 60 s | the server-side `agent.offline` check (§0.6) against `agents.last_seen_at` |
| `health-policy-reconcile` | 900 s | dispatch `health.policy.apply` where `device_health_policy_ack.policy_version` (§2.11) is behind the device's effective `health_profile.policy_version`, **and** where no ack row exists at all (which reads as version 0) |
| `health-retention` | 86400 s | delete `health_alert` rows past `alert_retention_days`; prune `health_flap_state` rows for keys with no alert in a flap window; **then hard-delete retired `health_check` rows (§2.4) that no alert still references** — otherwise soft-deleted checks are immortal |

Intervals are project constants, not per-org configuration — the SLA sweep's
reasoning applies unchanged: detection granularity is an operational property,
and a per-org interval lets one org's setting delay another's escalation.

`health-policy-reconcile` carries **§5.2's known limit**: it decides "never
armed" from the *ack* row, so a device holding a sent-but-unacknowledged policy
whose assignment is then deleted is skipped, and converges only after it
acknowledges. Read §5.2's amendment before treating a device that keeps
evaluating a deleted profile as a bug in the resolver.

`register_worker_job_types()` gains this module's registration call, for the
reason its docstring already gives: the worker process dispatches jobs
(`health.policy.apply`, remediations) without importing the API's registration
path, so without it the first sweep would raise `UnknownJobType`.

---

## 13. Frontend (`frontend/src/modules/health/`)

TanStack Query, strict TypeScript, no `any`; no new frontend pattern.

- **Alert console** — the live list, filterable by severity/state/device/group,
  sortable by `flapping` and `flap_count`. `unavailable` counts are shown as a
  distinct band, never folded into "healthy" (§0.7).
- **Alert detail** — the full context, the action rows and their states
  (including `blocked_rate_limit`, with its reason), the linked ticket and job,
  `previous_alert_id`'s ticket number, and the monitoring deep link (§0.12).
  A `require_approval` action renders approve/refuse for holders of
  `health.remediate` and renders the pinned script's name and version — nobody
  approves a script they cannot identify.
- **Profiles** — checks as a table with the effective duration rendered in both
  ticks and minutes (§0.3), and the per-OS availability matrix inline so an
  operator sees before saving that a BitLocker check will be `unavailable` on
  their Linux estate.
- **Device health** — the effective profile, **the assignment that produced it**
  (§0.2), each check's current state and value, and `policy_version` drift if
  the agent has not acknowledged the current policy.
- **Approved actions** — the registry, with `destructive`, `risk_class` and
  `autonomy` on the list view. `autonomy='autonomous'` is rendered
  distinctly, always: a page where "runs itself on 400 machines" looks like
  every other row is a page that gets clicked past. **Script exemptions (§2.10)
  are a first-class view on this page** — script name, pinned version, grantor,
  reason and grant time — because an exemption outlives the action that motivated
  it, and an invisible standing exemption is one nobody revokes.
- **Reporting** (§7.4's obligation): alert volume, MTTR and auto-remediation
  success rate **must** separate machine-resolved from human-resolved tickets
  and must exclude `cleared_reason='unavailable_timeout'` from any resolution
  count. A dashboard that conflates them is a defect. **As built,
  `HealthReportPanel.tsx` cannot compute §7.4's split**: the ticket list carries
  no `origin` filter and status history is a per-ticket read with no aggregate,
  so the panel splits on the alert's own `cleared_reason` instead and **says so
  on the page**. See §7.4's known limit (including the correction to that
  panel's stated reason). The exclusion half of the obligation
  (`unavailable_timeout`, plus `policy_removed` and `device_inactive`, each
  shown as its own number rather than dropped) *is* met.

---

## 14. Testing requirements (test-first)

Security- and correctness-critical paths get explicit tests:

**Tenancy** — one behavioural cross-org test per new table, each failing if its
`org_id` predicate is removed. Ingest with a device from another org's
connection binding rejects and audits.

**RBAC** — router-derived completeness (`route_permission`); a
`health.admin`-only user is refused `POST /alerts/{id}/remediate`; a
`health.remediate` user is refused the `approved-actions` write routes.

**The signed path** — a remediation produces a `jobs` row with a non-NULL
`signature` over the canonical bytes; no code path in `app/modules/health/`
reaches a device other than through `JobEngine.create`. Asserted structurally by
grepping the module for `publish_job`/`send_text` call sites in a test, the way
the notification module pins its single-egress-path property.

**The autonomy gate** — the CHECK constraint refuses `autonomous` + `destructive`
at the database layer, not only at the API; `is_destructive` returns true for
`script.run` with no exemption, and false only for the exact exempted
`(script_id, script_version)`.

**Idempotency** — the same breach report delivered five times produces one
alert, one ticket, one job. A sweep re-run after a crash mid-action produces no
second ticket. Both assert against the two unique indexes.

**The clear predicate, clause by clause** — six tests, one per `cleared_reason`,
plus the negative: **an offline device does not clear its open alerts**.

**Flap suppression** — the algorithm's boundary (`open_count` exactly at
`flap_threshold` does not suppress; one more does); a suppressed alert is still
**written** with `flapping=true` and its actions are `blocked_flapping`.

**`unavailable`** — never opens an alert, never clears one before the timeout,
clears one after it with the right `cleared_reason`.

**The monitoring boundary** — `test_no_health_table_grows_with_time_for_a_stable_device`:
100 summaries for one device, every row count unchanged (§0.12).

**Silent-by-design checks (§7.3's amendment)** — the observable is a **delivered
recipient count**, never a payload flag. With an org-wide rule for
`health.alert_raised` that resolves to at least one recipient: an alert from a
check *with* a `notify` action produces a `domain_events` row and
`len(resolve_recipients(...)) >= 1`; an alert from a check *without* one produces
**zero `domain_events` rows** for that alert, hence zero recipients, while its
`health_alert` row, its action rows and its audit records are all still written.
A separate test asserts `health.remediation_requires_approval` **is** published
for a silent check. Asserting `payload["notify"] is False` is forbidden: it passes
against the defect this amendment fixes.

**Amendment — these tests exist now, and what they had to vary.** They were not
writable until §2.2's third amendment gave the declaration a column: every check
declared `notify`, so no silent check could be constructed.
`tests/test_health_action_declaration.py` carries them, plus §6's
escalation-skip case. Two properties of their construction are required of
anything added there, because this project's recurring defect is a guard
satisfied by a **constant parameter** rather than by the guard:

- **the loud and the silent check live in the same org, on the same device, at
  the same age**, and both are asserted in one test. A test that seeded only the
  silent check passes against a module that publishes nothing at all;
- **the check *type* is swapped by parametrization** (`cpu.busy_pct` silent /
  `memory.used_pct` loud, and the reverse), so all four
  `(check_type, declares_notify)` combinations are covered and nothing keyed on
  the type can pass. Every check in `test_health_ingest.py` and
  `test_health_sweep.py` is `cpu.busy_pct` with `os_family=NULL`, so neither file
  can see a bug keyed on either.

**The escalation-skip test asserts the unstamped latch, not only the event
count.** A sweep that skipped the publish but still stamped `escalated_at` would
satisfy an event-count assertion, look correct indefinitely, and have silently
consumed the latch — so the day someone gives that check a `notify` action, §12
believes it has already escalated and never mails.

**Remediation stays off by default, and that is asserted as a pair.** One test
runs both values of the only parameter that decides it: a check with no
`remediate_approved_action_id` mints **no** `remediate` row at all (nothing for
the worker to pick up, so no signed job reaches an endpoint by this path), and
the same check with one mints exactly one row **carrying that action's id**. The
negative alone would pass against a build in which remediation cannot be armed by
anyone; the positive is what makes the negative a fact about the declaration.

**Check retirement and the `check_id` FK (§2.4's amendment)** — retiring a check
clears its open alerts `policy_removed` and leaves the alert rows with a
resolvable `check_id`; a hard `DELETE` of a check with alerts raises at the
database (`ON DELETE RESTRICT`, asserted on the admin engine) and the API returns
409 naming them; re-adding the same `(check_type, selector, os_family)` after a
retire succeeds — which fails if the unique index was not made partial — and mints
a **new** `check_id`; the 32-check trigger counts live rows only, tested with
retired rows present.

**The policy ack (§2.11)** — a device with no ack row is re-pushed by the
reconcile sweep and its breaches are **not** discarded; a device whose ack matches
gets zero jobs (assert a row count, not the absence of an exception); a
late-arriving result handler does not lower a stored `policy_version`.

**Notification obligations** — the two existing set-equality tests
(`_EMAIL_RENDERERS`, `_EVENT_TYPE_SAMPLE_PAYLOADS`) pass with four new types;
the four staff-only CHECK constraints refuse a `requester` rule at the database
layer.

**Wire protocol** — an oversized `reports` list, an unknown `kind`, an unknown
`state` and an extra field are each rejected at the parse layer, so nothing
unrecognised reaches business logic.

**Idle budget** — **written, and running in CI.** `agent/internal/capabilities/health/budget_test.go`
(8) and `budget_linux_test.go` (1) hold **nine** tests covering §0.5's six
numbered falsifiers — falsifier 2 has three of them, one Linux-tagged — plus
`TestTheTickEngineIsDormantUntilAPolicyArrives`. `posture_budget_test.go` adds
**six** more for the posture pass (falsifiers A–E, plus a check-order
starvation test). Counted by name on 2026-08-14 with `go test
./internal/capabilities/health/ -list` against the CI step's own `-run`
pattern, it matches **15** tests in this package on Linux. This figure moves
whenever a test matching that pattern is added or renamed — most recently by
`TestTheAllocationFreeCalcFunctionsAllocateNothing` (formerly
`TestTheCalcLayerAllocatesNothing`) — so treat it as a measurement to re-run,
not a constant to trust. They assert RSS delta
and per-tick allocation count against §0.5's falsifiers. The benchmark lives beside the capability under
`agent/`, as this line originally called for, and `.github/workflows/ci.yml`'s
fourth job, `agent`, runs it on every push — its "idle budget — the numbers,
in the log" step matches the falsifier tests with `-run 'Falsifier|...'` and
prints the measured numbers into the build log.

*Superseded 2026-08-13, quoted rather than deleted so the correction is
legible:* "**not written**, and named here so it is not lost: a benchmark
asserting RSS delta and per-tick allocation count against §0.5's falsifiers.
That benchmark belongs beside the capability under `agent/`, in this repository,
and needs the Go CI job `CLAUDE.md` now requires. **Today's CI covers none of
it** — see §0.5's amendments, which verify that `.github/workflows/ci.yml` has
no Go toolchain and no benchmark of any kind. Nothing in this list makes §0.5's
numbers true; only that benchmark, once it exists and runs, can." That last
sentence cited §0.5's amendments as support for a claim §0.5 itself retracts:
its third amendment (2026-08-12) states plainly that "there is no Go toolchain
... in this repo" is **false** — `.github/workflows/ci.yml` has a fourth job,
`agent`, pinning Go 1.24 and running `gofmt`, `go vet`, `staticcheck`,
`go test ./...`, six cross-compile targets, the packaging build, and an
idle-budget step — and its fourth and sixth amendments confirm all eight
falsifier tests exist and pass. Verified directly against
`.github/workflows/ci.yml` (2026-08-13), independent of §0.5's prose: it
defines four jobs (`backend`, `deploy`, `frontend`, `agent`); the `agent` job's
`cross-compile` step lists six targets (`windows/amd64`, `windows/arm64`,
`linux/amd64`, `linux/arm64`, `darwin/amd64`, `darwin/arm64`); and its
`idle budget — the numbers, in the log` step runs
`go test ./... -count=1 -v -run 'Falsifier|Idle|Allocat|Accumulat|Dormant|CostsABounded|MagicPacketCosts|Retained|LeakNoGoroutines|OpensNoSocket'`.
The eight named tests (`TestFalsifier1_RSSDeltaOnTheDefaultProfileStaysUnder2MB`,
two `TestFalsifier2_*` in `budget_test.go` plus a third in
`budget_linux_test.go`, `TestFalsifier3_PostureProbesNeverRunOnTheFastTick`,
`TestFalsifier4_AQuietTickWritesNothingToTheGateway`,
`TestFalsifier5_NoEventlogCheckOpensItsOwnLogSubscription` and
`TestFalsifier6_APolicyOverTheCapsNeverArmsTheTickEngine`) exist verbatim in
those two files.

---

## 15. What the existing kernel and code required, contradicted, or already provides

Read from the code, not inferred. Nothing here is designed around; it is
surfaced.

### 15.1 Kernel changes this module requires

1. **A new agent→server wire message kind, `health_report`**
   (`app/gateway/protocol.py`'s `AgentMessage` union, plus a branch in
   `app/gateway/dispatch.py::handle_agent_message`). This is unavoidable: the
   only agent→server kinds today are `job_status`, `job_log`, `job_result`
   (all job-bound), `heartbeat`, and `security_events`. Health reports are not
   job-bound, because §0.4 rejects the per-tick job on arithmetic. The precedent
   is exact — Phase 2 added `SecurityEventsMsg` and its `handle_agent_message`
   branch the same way — and the change adds no new *trust* surface: identity
   still comes from the mTLS binding and the parse layer still rejects unknown
   kinds.
2. **A kernel-owned `approved_action` registry** in `app/core/` — **two tables**,
   `approved_action` (§2.9) and `approved_action_script_exemption` (§2.10) — for
   the reason `app/core/events.py` states for the outbox: Phase 17 must share it.
   The exemption table is kernel-owned for the same reason as the registry: it is
   read by `is_destructive`, and a derivation function shared between phases must
   read a row shared between phases. This module defines both and registers into
   them; it does not own them.
3. **Four event types** in `app/core/events.py`, with §9's four-part obligation
   each.
4. **Five permission codes** in `app/core/rbac.py`.

**No change is required to:** the job engine, `SigningService`, canonical bytes,
the agent-side signed-job runner, RLS/`org_session`, the audit chain, or the
notification module's delivery/routing code. **Nothing in this module needs an
invariant relaxed or the idle budget exceeded.** The one place the design was
pushed by an invariant rather than by preference is §0.4 — the policy travels as
a signed job specifically because no unsigned server→agent channel may exist —
and that constraint made the design better, not worse.

### 15.2 Where the code contradicted the brief or the docs

- **`docs/modules/service-desk/spec.md` §7.2 says the alert→ticket contract is
  "reserved, not built".** It is built. `tickets.create_ticket` takes
  `origin`/`source_alert_id` as keyword arguments, validates
  `origin='alert' ⇒ type='incident'`, forces `requester_id`/`created_by` to
  `NULL` for alert origin, and refuses `source_alert_id` for other origins;
  `ck_ticket_human_attribution` and `ck_ticket_origin_valid` (migration 0024)
  back it in SQL, and `ticket.source_alert_id` is a real column. That section
  also calls this "Phase 15" while `docs/06-roadmap.md` places it at 16. This
  spec consumes what exists; §18 notes the doc edits.
- **The brief says remediation reuses existing job types "(script, service
  restart, cleanup, deploy)".** There is no service-restart job type and no
  cleanup job type. The registered set is `hello`, `inventory.collect`,
  `logs.ship`, `patch.scan`, `patch.apply`, `app.deploy`, `script.run`,
  `backup.run`, `backup.restore`, `provision.enroll`, `remote.session.open`. So
  "restart a service" and "clean up disk" are `script.run` against **pinned,
  versioned, org-authored scripts** — which is why §8.2's version pinning and
  §8.4's destructive-by-default treatment of `script.run` carry as much weight
  as they do. The brief's "no new action code" still holds; its list of existing
  actions does not.
- **The brief says inventory supplies security-posture facts to evaluate
  (encryption, AV, firewall).** `InventoryDocument` (`app/modules/inventory/
  schema.py`) has `facts: dict[str, Any]` — free-form, with no pinned posture
  keys and no guarantee any agent populates them. Reading posture from inventory
  would mean depending on an unspecified contract *and* moving evaluation off
  the edge, which the brief's own boundary forbids. Resolved: the `posture.*`
  checks are **agent-evaluated check types on the slow posture sub-tick**
  (§4.1). If a later phase wants typed posture facts in inventory, that is an
  inventory spec change, not a dependency this module takes.
- **`docs/05-agent-spec.md` says enrollment returns a resolved policy.**
  `EnrollResponse` has no policy field and no server→agent policy channel
  exists. §0.4 routes policy over the signed-job path instead.
- **`app/core/events.py` lines 79–83 describe routing behaviour
  `app/modules/notification/routing.py` does not implement** — see §9.2. The
  constraint it justifies is still correct; the stated mechanism is wrong, and
  `delivery.py`'s own docstring states the mechanism correctly.

---

## 16. Migration notes

Forward-only. **The migration number and `down_revision` are determined at build
time, not here** (§2's amendment): run `alembic heads` and `ls
migrations/versions/`, take the next free number, and chain off the **actual**
head. An earlier draft of this section said "migration `0040`, head is `0039`";
`0039_sla_breach_sweep_indexes.py` was written by a concurrent agent while this
spec was being written and the head has moved since. Treat any number written in
a document as stale. The work may land as one migration or several — the
implementation plan splits it into kernel (events, permissions, notification
CHECKs), registry (`approved_action` + exemptions) and module (the health
tables), chained in that order — and this spec does not fix that choice.

**As built** (see §2's "As built" note for why this is recorded afterwards and
not before): `0040_health_kernel` → `0041_approved_actions` → `0042_health`, and
then `0044_health_check_action_declaration` a sub-project later.

Contents:

- **nine module tables plus the two kernel tables** (`approved_action`,
  `approved_action_script_exemption`), each with RLS `ENABLE + FORCE` and a
  `tenant_isolation` policy carrying `USING` and `WITH CHECK`;
- **a later, separate migration adding §2.2's three action-declaration columns**
  (as built: `0044_health_check_action_declaration`, chained off IPAM's `0043`)
  to `health_check` (`declares_notify`, `declares_open_ticket`,
  `remediate_approved_action_id` with its `ON DELETE RESTRICT` FK to
  `approved_action`). Additive only: the booleans carry `server_default true` so
  no existing row changes behaviour, the FK column is nullable so no existing
  row gains a remediation, and no shipped migration was edited. It adds no
  index, so §16's index-declaration trap below does not apply to it — but the
  columns must still be declared in `HealthCheck` for the model/schema drift
  test, which compares nullability;
- the partial unique indexes of §0.2, §0.8, §2.2 (`WHERE retired_at IS NULL`),
  §2.5, §2.6, §2.7;
- `health_alert.check_id` as `ON DELETE RESTRICT` (§2.4);
- the 32-checks-per-profile statement trigger, counting `WHERE retired_at IS
  NULL` (§2.2);
- five permission codes and their role grants;
- four event types (a code change in `app/core/events.py`, listed here because
  the migration and the registry must land together);
- four `ck_rule_no_health_*_to_requester` CHECK constraints on
  `notification_rules` (§9.2), following migrations 0025 and 0034.

No existing column is altered and no shipped migration is edited.

`test_schema_drift.py` compares `Base.metadata` to the live schema and
deliberately does **not** compare indexes, so every partial unique index above
must be declared in `__table_args__` **as well as** in the migration —
otherwise `alembic revision --autogenerate` would propose dropping all of them,
including `uq_health_alert_open_key`, which is the guarantee the whole alert
lifecycle rests on. This is the trap `docs/modules/sla/spec.md` §11 hit and
documented.

---

## 17. Deferred (explicitly not in v1)

- **Composite / correlated alerts** (multiple conditions, cross-device
  correlation) — needs an expression model, which §0.1 rejects for v1.
- **Maintenance-window suppression.** `app/core/schedule.py::window_open`
  already exists and is the obvious mechanism; the design question deferred is
  whether a suppressed breach is *not opened* or *opened and not actioned*
  (§0.9 argues for the latter), and it deserves its own decision.
- **Dependency-aware muting** ("the host is down, stop alerting on its
  services") — needs a dependency graph this platform does not have.
- **Predictive flags** (disk-fill rate, memory-leak trend) — requires a series,
  which is §0.12's boundary. Coordinated with the monitoring product.
- **Multi-step remediation playbooks** — overlaps the AI layer's orchestration;
  build once, in the shared substrate, not twice.
- **Operator-authored event-log queries** (§4.1) — the free-form expression
  problem in another shape.
- **Per-key mute** (mute this check on this device indefinitely) — today a mute
  is per-alert and bounded to 30 days, on purpose (§0.8).
- **Group-target remediation** — `max_target_scope` exists for it; v1 CHECKs it
  to `'device'`.
- **Per-check hysteresis bands** (§0.9).
- **SNMP, agentless checks, network topology, time-series retention** — the
  monitoring product's domain, permanently, not "later".

---

## 18. Documentation changes made with this spec

All four landed with the build; recorded here as done rather than owed.

- `docs/modules/service-desk/spec.md` §7.2 — retitled "Phase 15" → "Phase 16"
  and "reserved, not built" → **BUILT**, with a pointer at this spec's §7.4 for
  the behaviour and the named SQL CHECKs (`ck_ticket_origin_valid`,
  `ck_ticket_human_attribution`) that carry `origin='alert'`. The stale "Phase
  15" references elsewhere in that spec (§1's monitoring boundary, §2's
  `origin`/`source_alert_id` bullets, §3's machine-transition rule) were
  corrected in the same pass, and §7.2 now also records why the reporting split
  §7.4 requires is not computable from *that module's* API — no `origin` filter
  on the ticket list, and status history exposed per ticket with no aggregate.
- `docs/05-agent-spec.md` — `health.check` added to the capability table with a
  full contract subsection (policy only by signed job, empty check list =
  disarm, fixed-size per-check state, edges not state, `agent.offline` never
  pushed, `eventlog.*` riding the security-logs subscription, remediation not
  its business); the `health_report` wire kind documented with its four
  load-bearing properties; and the enrollment step corrected — `EnrollResponse`
  carries six fields and no policy, and there is no policy channel, because
  policy reaching an agent by anything other than a signed job is invariant #1.
  The idle-budget accountability note (§0.5's amendment) is restated there too.
- `docs/06-roadmap.md` — Phase 16 marked **BUILT (control plane)** in the phase
  table, with a status note saying plainly what is *not* built: the agent
  capability is unwritten and has no coverage in this repo's CI, so §0.5's
  numbers are a design target, not a measured result. (Both that note and
  §0.5's amendment were re-stated on 2026-08-05 when the agent moved in-tree —
  the wording had attributed the coverage gap to the agent living in a separate
  repository, which is no longer a reason for it.) *That description of
  `docs/06-roadmap.md`'s status note is itself now historical: the agent
  capability, including the posture probes, is implemented, and the roadmap's
  Phase 16 cell and status note were corrected on 2026-08-13 to say so — see
  this spec's fifth amendment (§0.5) for the verified, platform-by-platform
  state.*
- `docs/kernel/wire-protocol/` — regenerated after adding `HealthReportMsg`,
  `HealthPolicyApplyParams` and `HealthCheckNowParams` to
  `scripts/export_wire_schema.py`.

**Two known limits are recorded where a reader hits them**, not in a list nobody
opens: the assignment-delete disarm gap at §5.2 (cross-referenced from §12), and
§7.4's reporting split not being computable from the current API (cross-
referenced from §13 and from the service-desk spec's §7.2).

---

## 19. Related documents

- `docs/modules/endpoint-health-alerting.md` — the scoping brief this expands.
- `docs/06-roadmap.md` — Phase 16 row and the "Note on Endpoint Health &
  Alerting" (idle budget, monitoring boundary, remediation governance).
- `docs/modules/sla/spec.md` — §0's house style, §3's resolution order and its
  partial-unique-index enforcement, §7's idempotent sweep, §11's schema-drift
  trap.
- `docs/modules/notification/spec.md` — the outbox, routing, delivery and the
  egress guard this module publishes into and never re-implements.
- `docs/modules/service-desk/spec.md` — §7.2's alert→ticket contract, §3's
  lifecycle, §8's invariants.
- `docs/modules/ai-admin.md` — the persona set that must join §2.9's registry.
- `docs/03-security-model.md`, `docs/05-agent-spec.md` — the invariants and the
  idle budget this spec applies.

---

## 20. CLOSED: `POST /health/alerts/{id}/remediate` was gated on one permission where it needs two

**Found by, and recorded from, the AI Automation build**
(`docs/modules/ai-automation.md` §0.11), recorded here unfixed, and **fixed in
this module's own cycle** — which is where it always belonged, because it turns a
currently-succeeding call into a 403 and edits this module's test suite. The
defect is described below as it stood, unedited, because a fix section that
paraphrases what it fixed is a section nobody can check; the fix and its
evidence follow it.

### The defect (as it stood, unedited)

`app/modules/health/api.py::remediate_alert_route` takes
`require_permission("health.remediate")` and then dispatches by
`body.action_key`. The catalogue row that key names carries a `job_type`, and
that job type has its own module dispatch route with its own permission —
`patch.apply` for `patch.apply`, `deploy.script.run` for `script.run`,
`backup.run` for `backup.run`, and so on. **Nothing checks it here.**

So a holder of `health.remediate` alone can dispatch a `patch.apply` job through
this route while being refused the same job on `POST /api/v1/patch/apply`. That
is privilege escalation across module boundaries, and it is invariant #6
violated: authorization is server-side, but the wrong authorization.

It is bounded, and the bound is why this is a defect rather than an incident:
the action must already exist in the org's `approved_action` catalogue, authored
by a `health.action.admin` holder through an audited write path, and the rate
fuses and script-version pin still apply. The escalation is over *which existing
approved action a `health.remediate` holder may fire*, not over what may run at
all.

### The fix, as built

`app/jobs/dispatch_permissions.py` already existed (built by the AI Automation
layer, which needed the same map for its own confirm gate). It gives
`dispatch_permission_for(job_type) -> str | None`, deny-by-default, with its
partition against the live job-type registry asserted by a test.

`app/modules/health/actions.py::_require_dispatch_grant` is that map applied
here. Both dispatching paths call it against the resolved `approved_action` row,
**in addition to** the route's `health.remediate`:

* `dispatch_manual_remediation` — `POST /health/alerts/{id}/remediate`
* `approve_action` — `POST /health/alerts/{id}/actions/{action_id}/approve`

`refuse_action` deliberately does **not**: refusing creates no job, so there is
no dispatch to authorize, and requiring the second grant to decline would leave
approval requests nobody in the org may clear sitting until `approval_ttl_s`
expires them. That is the same reasoning that gates
`POST /ai/actions/{id}/reject` on `ai.act.propose` rather than `ai.act.confirm`.

**`None` IS A REFUSAL.** `dispatch_permission_for` answers `None` both for a job
type in `NO_HUMAN_DISPATCH_PATH` — all three of `health.policy.apply`,
`agent.cert.renew`, `agent.cert.install`, each dispatched by a kernel state
machine from state the kernel itself minted — and for one nobody has classified.
"There is no route, so there is no grant to check" and "nobody has said which
grant applies" are both refusals; reading either as "no permission needed"
inverts the map and turns this fix into a bypass wider than the hole it closed.

**THE JOB TYPE AT DISPATCH IS THE AUTHORITY, NOT THE ONE THAT WAS APPROVED.** The
check reads `action.job_type` as it stands in the same transaction that creates
the job, for the reason `_rederived_destructive` re-derives `destructive` instead
of trusting the stored column: a value a write gate checked when the row was
authored is not authority at dispatch time. A catalogue row edited from
`inventory.collect` to `patch.apply` after approval dispatches as `patch.apply`
and is gated as `patch.apply`.

**It could not be a route dependency.** The permission required depends on the
REQUEST BODY's resolved job type, which no `Depends(require_permission(...))`
can see — so it is a service-layer check, and
`app/core/rbac.py::route_permission`'s router-derived completeness test still
sees only `health.remediate` on both routes. That is correct rather than a gap,
and it is commented at both call sites: the route-level gate is the floor, not
the whole check, exactly as it is for `POST /ai/actions/{id}/confirm`.

**403, not 404**, which is the one deliberate exception to §10's "not-mine is
404". It is raised only after the alert AND the approved action have both
resolved in the caller's own org, so it confirms the existence of nothing a
`health.remediate` holder could not already read; a 404 would tell them their own
catalogue entry had vanished. `HealthForbiddenError` carries it.

### What changed for operators, and what did not

`health.remediate` is seeded to super-admin and org-admin only (migration 0040),
and both hold every permission in the platform, so **no seeded role loses a
capability**. What changes is a hand-built role granting `health.remediate`
without the job types' own gates: its holder now gets a 403 naming the missing
code instead of a signed job. That is the point.

The bound on the escalation this closes is unchanged and worth restating: the
action still had to exist in the org's `approved_action` catalogue, authored by a
`health.action.admin` holder through an audited write path, with the rate fuses
and the script-version pin applying. What was escalated was *which existing
approved action a `health.remediate` holder may fire* — now it is only the ones
they could fire by hand.

### Evidence

`backend/tests/test_health_remediation_authz.py`. Every test asserts the `jobs`
table and the action row, not only the status: "403" and "403 but it also
dispatched" are different facts, and "403" and "403 but it burned the alert's one
`remediate` slot" are two more. Four mutations were applied to the implementation
and each produced a failure naming a wrong value:

| Mutation | What it printed |
| --- | --- |
| Delete the check in `dispatch_manual_remediation` | `(200, 1, ['patch.apply'])` |
| Delete the check in `approve_action` | `(200, 1, 'dispatched', <job id>)` |
| Treat `None` as a pass | `(200, 1)` — an `agent.cert.renew` job, dispatched from the alert console |
| Accept ANY permission in the map | `(200, 1)` on the three tests that vary *which* grant is held |

The `None`-is-a-pass row is parametrized over all three
`NO_HUMAN_DISPATCH_PATH` entries read from the live map, so a fourth entry lands
in the test without anyone remembering to add it. `agent.cert.renew` is the entry
that makes it discriminate: its params model is empty, so under the inverted
reading the call does not merely fail validation — it mints a job.

### Why the AI Automation build did not fix it

It is a **behaviour change to this module's shipped authorization**: it turns a
currently-succeeding call into a 403, so it edits this module's test suite.
Landing that inside a sixteen-task refactor of another layer means a security fix
nobody reviews on its own, in a diff nobody reads to the end — which is the same
argument §0.11 of the automation spec makes about why the map should be data
rather than prose. The judgement held: fixing it here took two service-layer
calls and one error class, and the review is this section.

**What that build did guarantee is that this layer did not inherit the gap.**
`ai.act.confirm` is checked together with `dispatch_permission_for(job_type)` and
a loopback read of the target device, and
`tests/test_ai_act_confirm_authz.py` and `tests/test_ai_act_api.py` both assert
the 403 **and the unchanged `jobs` count** — "403" and "403 but it also
dispatched" being different facts.
