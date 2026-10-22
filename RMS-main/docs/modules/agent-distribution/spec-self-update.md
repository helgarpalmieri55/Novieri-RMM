# Agent self-update: spec

**Status:** built 2026-09-23 (agent, server module, console, the
`agent-update-e2e` workflow and the release-signing workflow); see
"Implementation notes" at the end for where the build refined this design.
The per-OS behaviour is proven only by `agent-update-e2e` runs, and until one
is green on a runner the row for that OS stays "never executed". Designed
2026-09-23. The operator approved it on
2026-09-23: a signed `agent.update` job, pinned to a release SHA-256, with
local rollback if the new version does not check in, rolled out in rings. That
decision replaces the refusal recorded as OQ-3
(`agent/packaging/README.md`, "Self-update is now a signed job";
`docs/modules/agent/open-questions.md` OQ-3). Everything below is a design
target until the falsifiers in §11 run in CI on all three operating systems.

**What this spec does not claim.** Nothing here has run on Windows or macOS.
The swap and rollback mechanics in §5 rest on documented service-manager
behaviour. §11's end-to-end job is what will confirm or refute that behaviour,
and until that job is green on `windows-latest` and `macos-latest` the per-OS
rows stay marked "never executed".

---

## 1. Why this is allowed now, and what it costs

The old refusal argued that an agent which downloads a binary and checks a
hash supplied by the control plane lets a compromised control plane replace
root/SYSTEM code "while vouching for itself". That is true. It is equally true
of two job types that already ship:

- `script.run` runs an arbitrary signed body as SYSTEM/root
  (`agent/internal/capabilities/deploy/script.go:11-20`).
- `app.deploy` downloads an artifact, checks it against a SHA-256 the control
  plane supplied, and runs it as SYSTEM/root
  (`agent/internal/capabilities/deploy/install.go`, `stage`).

A control plane that can sign jobs can already replace the agent, today, with
a two-line `script.run`. **`agent.update` therefore gives a signing control
plane no power it lacks.** What it adds is a supervised, reversible,
version-aware way to use that power, which the fleet does not have. Without it,
every agent defect, including a defect in signed-job verification, is fixed by
touching every machine by hand.

What self-update genuinely adds is a **new availability risk.** A bad release
can take the management channel down on every endpoint it reaches, and a
disconnected agent cannot be told to roll back. The design spends its effort on
that risk:

- a local, time-boxed health gate with rollback performed on the endpoint (§5);
- a pilot ring capped at 25 devices, human-advanced rings, and a failure
  threshold that pauses the rollout (§6);
- a post-update signed `hello` to prove the new binary can still verify and run
  a job (§6.4).

Downgrade is the second new risk: reinstalling a release with a known flaw. It
is handled in §4.3 and §7.

---

## 2. Invariants, checked one by one

| Invariant | How it holds |
|---|---|
| 1. Signed jobs only | `agent.update` is an ordinary `JobEnvelope`. The runner verifies signature, device, freshness, nonce and type before the handler exists for it. The helper process (§5.2) takes no network input: it reads a state file that only a verified handler writes, in a root-only directory. |
| 2. Outbound only, no listening port | The download is an outbound HTTPS GET. The helper and the agent coordinate through files in the data directory. There is no socket, named pipe or RPC endpoint. §11 asserts that no listening socket exists during any phase. |
| 3. Credentials | None are involved. The artifact is identical for every tenant. The presigned URL's query string is removed from API responses and audit metadata by a `params_redactor` (§8.2). |
| 4. Audit | Every rollout action, every dispatch, every per-device outcome and every observed version change is written to the hash-chained log (§8.4). |
| 5. Tenancy | Rollout tables carry `org_id` under RLS. `agent_releases` stays global, as before (`spec.md` §4.2), and stays writable only by `super-admin`. |
| 6. RBAC | New codes, checked server-side on every route (§8.3). `agent.update` is never approvable by an automated or AI actor (§8.5). |
| Idle budget | Nothing runs until a job arrives. With no update pending, the only cost is one `os.Stat` at process start. During the trial window only, there is one timer. The helper exists only during an update (§9). |
| Wire contract | The change is additive in both directions (§3.4). |

---

## 3. Wire changes

### 3.1 New job type `agent.update`

Its params are `AgentUpdateParams`, exported to
`docs/kernel/wire-protocol/AgentUpdateParams.schema.json`. The params are per
device, so each job carries the artifact for that device's platform only.

| Field | Type | Rule |
|---|---|---|
| `target_version` | string | Strict semver `MAJOR.MINOR.PATCH[-pre]`, at most 50 characters, no `+build` suffix. One leading `v` is stripped on both sides. |
| `os` | `linux\|windows\|darwin` | Must equal the agent's `runtime.GOOS`. |
| `arch` | `amd64\|arm64` | Must equal `runtime.GOARCH`. |
| `artifact_url` | string | `https://` only. It is a presigned GET for `agent_releases.storage_ref` (§8.2). |
| `artifact_sha256` | string | 64 hex characters, from `agent_releases.sha256`, which the server computed. |
| `artifact_size` | int | `agent_releases.size_bytes`, from 1 byte to 256 MiB. |
| `rollback` | bool, default `false` | Must be `true` for `target_version` < current. |
| `health_window_s` | int, default 600 | From 120 to 3600. How long the new binary has to confirm (§5.3). |
| `rollout_id` | string \| null | Used in logs only. The agent attaches no meaning to it. |

Unknown members are ignored (encoding/json's default), in line with the
wire-protocol README's rule for inbound messages.

The result is `AgentUpdateResult`. The runner's `{"reason": ...}` refusal shape
is unchanged.

| Field | Meaning |
|---|---|
| `outcome` | `updated` \| `already_current` \| `rolled_back` \| `aborted` |
| `from_version`, `to_version` | as the agent saw them |
| `sha256` | digest of the binary that is now running |
| `signature` | `verified` \| `not_required` (§10) |
| `starts` | how many times the new binary started during trial |
| `reason` | stable token for `rolled_back`/`aborted` (§4.4), sanitized and clamped by the runner |

`JobResultMsg.state` is **not widened**. `updated` and `already_current` are
reported as `succeeded`. `rolled_back` and `aborted` are reported as `failed`.
Refusals use `refused`.

### 3.2 The result is deferred, and delivered by whichever process finishes

The process that receives the job is not the process that knows the outcome.
The handler stages the download, launches the helper, and returns
`jobs.ErrDeferred`. This is a new sentinel in `agent/internal/jobs`, and it is
the one kernel change on the agent side. For it, the runner sends nothing
beyond the `running` status it has already sent, and logs the deferral. The
terminal `JobResultMsg` is then enqueued through the existing persisted
`Outbox` in one of two ways:

- **the new binary** enqueues it once it has confirmed (§5.3), as `succeeded`;
- **the old binary** enqueues it after a rollback or abort, on its first start
  afterwards (§5.4), as `failed`.

This is not new protocol. The outbox already delivers results produced before
a reconnect on a later connection. The server accepts a result for a
`running` job from the device the job targets, whichever connection it arrives
on. The job's `expires_at` is sized for this (§6.4). Only `agent.update` may
return `ErrDeferred`. A registry test enforces that, and for any other type the
runner converts it to `failed`.

### 3.3 Two optional headers on the WebSocket upgrade request

The agent adds `X-ITS-Agent-Version: 1.7.0` and
`X-ITS-Agent-Capabilities: agent.update,app.deploy,...` (sorted,
comma-separated, at most 4 KiB) to the upgrade request
(`transport.Options.Header`, which is passed to `websocket.DialOptions.HTTPHeader`).
The gateway (`app/gateway/main.py::_handle`) reads both after it has
authenticated the certificate. It writes `agents.agent_version`,
`agents.agent_version_reported_at` and `agents.capabilities` in `_mark_online`.
Each capability name must match `^[a-z][a-z0-9._-]{0,63}$`. If one does not,
the header is ignored and not stored.

The headers are **self-reported by an authenticated agent**. They are used for
display and for eligibility, never for authorization, so a lying agent can only
misdescribe itself. Today `agents.agent_version` is never written: the column
exists and nothing fills it. This is what fills it.

Rejected alternatives for reporting version:

- **A field on `HeartbeatMsg`.** That model is `extra="forbid"`. A newer agent
  talking to an older control plane would have every heartbeat dropped at
  `parse_agent_message`.
- **A new `agent.describe` job on every connect.** That costs a signed job per
  connect to learn something the upgrade request can carry for free.

### 3.4 Compatibility, in both directions

- **New control plane, old agent.** An old agent sends no headers, so its
  version reads "unknown" and the rollout preview classes it as
  `needs_manual_update` without dispatching anything. If `agent.update` does
  reach it (for example, if stale `agents.capabilities` claimed support), its
  runner refuses `unknown_type` before anything runs, and the target becomes
  `needs_manual_update` (§6.3). The console shows both cases as **"Needs
  manual update"**.
- **New agent, old control plane.** The `websockets` server ignores unknown
  request headers, and an old control plane never sends `agent.update`. Nothing
  the new agent sends is rejected.
- **No enum is widened, no field becomes required, and no message changes
  shape.**

**Every agent deployed today predates this, so each needs one manual hop** to
the first update-capable release, through `dpkg -i`, `install.ps1`, GPO/Intune
or MDM (`agent/packaging/README.md`). This spec does not build a bootstrap. §12
asks whether one should exist.

---

## 4. The handler: everything checked before anything is fetched

The handler lives in the package `agent/internal/capabilities/selfupdate`,
under the job type `agent.update`. The registry line sits in `mustBuild()`,
next to the cert pair. Registration is a map insert.

### 4.1 Order of checks (each one is a refusal, and nothing is downloaded before step 10)

1. Params parse. Required members are present, and `artifact_sha256` is exactly
   64 hex characters (reuse `deploy.expectedDigest`'s strictness).
2. `os`/`arch` equal the runtime values → otherwise `platform_mismatch`.
3. The agent's own `main.version` and `target_version` both parse as semver →
   otherwise `version_not_orderable`. A `dev` or git-SHA build never
   self-updates. That is intended: release builds are semver-stamped by
   `build.sh VERSION=`.
4. Ordering (§4.3).
5. `<data>/update/state.json` is absent or terminal → otherwise
   `update_in_progress`.
6. No other job is executing (the new `Runner.Executing(excludeID) int`, read
   from the interrupt map) → otherwise `busy`. A swap restarts the process, and
   a `backup.run` or `patch.apply` half-done is worse than an update delayed.
7. The process runs under its service manager, and the service manager's
   configured binary path equals `os.Executable()` after symlink resolution →
   otherwise `not_running_as_service` / `unsupported_install_layout`. The check
   uses `systemctl show its-agent -p ExecStart` on Linux, `QueryServiceConfig`
   `BinaryPathName` on Windows, and `ProgramArguments[0]` from
   `launchctl print system/com.toeshee.itsagent` on macOS.
8. `artifact_url` is `https://`, and `artifact_size` ≤ 256 MiB → otherwise
   `bad_artifact_url` / `artifact_too_large`.
9. There is free space of at least 3 × `artifact_size` on the data directory's
   volume and on the target binary's volume (`unix.Statfs` or
   `windows.GetDiskFreeSpaceEx`; both live in `golang.org/x/sys`, which is
   already a dependency) → otherwise `insufficient_disk`.
10. Fetch (§4.2).

### 4.2 Stage and hand off

These steps reuse `app.deploy`'s pattern and code. `httpFetch` moves to
`agent/internal/fetch`, and `newScratch`/`restrictDir` move to
`agent/internal/secdir`, so both capabilities share one implementation of the
two security-relevant primitives.

1. Create `<data>/update/` with owner-only permissions: 0700 root on POSIX, and
   a protected DACL for SYSTEM and Administrators on Windows (`restrictDir`).
   Then create `staged/` and `prev/` inside it.
2. Stream the download into `staged/download.part`, capped at
   `artifact_size + 1`, while hashing. If the size or the SHA-256 does not
   match, delete the file and fail with `size_mismatch` / `hash_mismatch`.
   Compare in constant time, and never echo the digests (as `stage` does).
3. Rename it to `staged/its-agent[.exe]`, mode 0700.
4. Windows, once a signer pin is compiled in: verify Authenticode (§10).
5. Copy the running binary (`os.Executable()`) to `prev/its-agent[.exe]` and
   hash it. That copy is the rollback source and is also what runs the helper.
6. Run `prev/its-agent --version`, with a 10 s timeout. It must exit 0 and
   print `main.version`. If the data directory is mounted `noexec`, that shows
   up here as `data_dir_noexec`, before anything has changed.
7. Write `state.json` in phase `staged`. Write it atomically: a temporary file,
   `fsync`, then rename.
8. Launch the helper (§5.2). If the launch fails, fail with
   `helper_launch_failed`, delete `state.json`, and change nothing else.
9. Return `jobs.ErrDeferred`.

### 4.3 Version ordering

Both sides use one comparator, fixed by the shared vectors in
`docs/kernel/wire-protocol/testdata/semver-order.json`. Go
(`agent/internal/update/semver_test.go`) and Python
(`backend/tests/test_agent_update_semver.py`) must both pass on the same file,
the same way `canonical/testdata` pins canonical JSON.

| Case | Result |
|---|---|
| target > current | proceed |
| target < current, `rollback: false` | refuse `downgrade_not_permitted` |
| target < current, `rollback: true` | proceed |
| target == current, running SHA-256 == `artifact_sha256` | `succeeded`, `outcome: already_current`, no restart |
| target == current, digests differ | refuse `version_conflict`: two different binaries claim one version, which is a release-process defect, and nothing is swapped |

### 4.4 Stable reason tokens

The server reads these tokens. They are written in one place per side, like
`cancelled_before_start`.

- **Refusals:** `platform_mismatch`, `version_not_orderable`,
  `downgrade_not_permitted`, `version_conflict`, `update_in_progress`, `busy`,
  `not_running_as_service`, `unsupported_install_layout`, `bad_artifact_url`,
  `artifact_too_large`, `insufficient_disk`, `signature_required`,
  `signature_invalid`.
- **Failures:** `download_failed`, `size_mismatch`, `hash_mismatch`,
  `data_dir_noexec`, `helper_launch_failed`, `selftest_failed`,
  `install_failed`, `no_confirmation`, `crash_loop`, `interrupted`,
  `rollback_failed`.

---

## 5. Swap and rollback

### 5.1 One principle, three service managers

**The service definition never changes. The file at its configured path is
replaced by an atomic rename-over, and the previous binary is kept.** The
reasons:

- **macOS.** A PPPC/TCC grant on an ad-hoc-signed binary can only pin by path
  (`agent/packaging/README.md`, "Code signing" #3). A new path would silently
  lose the grant.
- **Windows.** Enterprise outbound-firewall rules, AV exclusions and
  WDAC/AppLocker path rules are usually keyed on the program path.
- **Linux.** A systemd drop-in pointing at a versioned directory would fight
  dpkg over `/usr/bin/its-agent`. A plain file at the same path is what
  `apt`/`dpkg -i` expects to overwrite later.

**A later manual install always wins.** A package install or `install.ps1`
overwrites the file, and the gateway records the version change (§8.4). No
state has to be reconciled. A leftover `state.json` in a terminal phase is
ignored.

The one install primitive, `update.InstallFile(src, wantSHA, target)`, is used
for both the swap and the rollback:

1. Copy `src` to `<targetdir>/.its-agent.new`, opened `O_EXCL`, then `fsync`.
2. **Re-hash `.its-agent.new` in the target directory** and compare it with
   `wantSHA`. This is the last check before the rename, done at the location
   the file will run from.
3. Run `.its-agent.new --update-selftest` with a 30 s timeout. The self-test
   loads config, loads identity read-only, parses the pinned job-signing key,
   builds the capability registry, and prints `{"version","os","arch"}`. It
   dials nothing and writes nothing. It must exit 0 and report the expected
   version. Architecture errors, missing ad-hoc signatures on arm64 macOS, AV
   blocks and init panics surface here, **before** the swap. If it fails, the
   result is `selftest_failed`.
4. Rename `.its-agent.new` over `target`. On POSIX this is `rename(2)`. On
   Windows it is `MoveFileExW(MOVEFILE_REPLACE_EXISTING|MOVEFILE_WRITE_THROUGH)`
   with the service stopped (§5.5). At no point is `target` missing.

The target directory holds `.its-agent.new` only for the length of one swap.
The previous binary lives in `<data>/update/prev/`, not beside the target, so
nothing unowned ever appears in `/usr/bin` or `Program Files`.

### 5.2 The helper: a transient process, running the previous binary

The helper is `prev/its-agent --update-helper=apply --state <data>/update/state.json`,
or `=rollback`. It is the **known-good binary**, so a new release that cannot
start cannot also break its own rollback. It must live outside the service's
lifetime, because it stops and starts that service:

| OS | How it is launched so a service stop does not kill it |
|---|---|
| Linux | `systemd-run --unit=its-agent-update --collect --no-block --quiet -p Type=exec -p ProtectSystem=strict -p ReadWritePaths=<targetdir> -p ReadWritePaths=<data> -p ProtectHome=yes -p PrivateTmp=yes -p NoNewPrivileges=yes -- <prev> --update-helper=apply --state ...`. This is a transient unit outside `its-agent.service`'s cgroup, which `KillMode=control-group` would otherwise kill. |
| macOS | `launchctl bootstrap system <data>/update/com.toeshee.itsagent.update.plist`, a root:wheel 0644 plist with `RunAtLoad`, no `KeepAlive` and `LaunchOnlyOnce`. The plist is not under `/Library/LaunchDaemons`, so a reboot does not relaunch it. The helper `bootout`s its own label as its last act. `launchctl submit` is the fallback if `bootstrap` refuses a path outside the standard directories, and the macOS e2e job (§11) settles which one ships. |
| Windows | `CreateProcess` with `DETACHED_PROCESS\|CREATE_NEW_PROCESS_GROUP\|CREATE_BREAKAWAY_FROM_JOB`, inheriting SYSTEM. If breakaway is denied, it retries without that flag. The SCM does not kill a service's child processes on stop. |

The helper reads one file and writes one file. It has no network access
(a Linux unit property `-p IPAddressDeny=any` is added), no socket and no
arguments beyond the phase and a path. The agent's Linux sandbox allows
`systemd-run`: it is a D-Bus client over `AF_UNIX`, running as uid 0.
**This is also true of every root process today.** The transient unit's own
hardening is set above, and the exact set it needs is measured by the Linux
e2e job, not reasoned about.

### 5.3 The state machine and the health gate

The state file is `<data>/update/state.json`. Every write is atomic.

```
staged ─▶ swapping ─▶ trial ─▶ confirmed                     (success)
   │          │          └──▶ rolling_back ─▶ rolled_back     (health gate failed)
   └──────────┴──▶ aborted                                    (failed before the swap took effect)
                                  rolling_back ─▶ rollback_failed (manual repair needed)
```

Fields: `job_id`, `from_version`, `to_version`, `sha256`, `prev_sha256`,
`target_path`, `trial_deadline` (set when `trial` begins: now plus
`health_window_s`), `starts`, `reason`, and `reported` (bool).

**Apply (helper):** `staged → swapping`, `InstallFile(staged, sha256, target)`,
restart the service (§5.5), then `trial`. Every 5 s the helper polls
`state.json` and the service state. Polling here is acceptable: this process
exists only during an update and is not the idle agent. It stops polling when:

- `confirmed` → delete `staged/`, keep `prev/`, and exit 0;
- `starts > 3` (crash loop), or the deadline passes → **rollback**.

**Confirmation (new binary).** `update.Resume(cfg, version)` is called in
`main.run` directly after `config.Load`. If `state.json` is absent, the cost is
one `os.Stat`. In `trial` with `to_version == version`, it increments `starts`
and persists it. If `starts > 3`, or the deadline has already passed, it
launches the rollback helper and exits non-zero. Otherwise it arms **one
`time.AfterFunc` for the remaining deadline** and registers a confirmation
hook. The binary confirms when **the mTLS WebSocket upgrade succeeded and that
connection stayed up for `transport.DefaultResetAfter` (60 s)**. That is the
same definition of "a connection that counts" the transport already uses to
reset its backoff, so a certificate the gateway refuses, a 4401/4403, or a
crash-on-first-frame never confirms. On confirmation, the new binary enqueues
the `succeeded` result through the outbox, then writes `confirmed`, then stops
the timer, in that order. If the deadline timer fires first, the binary
launches the rollback helper and exits. After confirmation, `Resume` does
nothing on later starts until a new job writes a new state file.

**Only one party decides.** The helper's watchdog and the new binary's own
deadline can both decide to roll back. Whichever creates
`<data>/update/decision.lock` with `O_EXCL` first acts, and the other stands
down. Confirmation takes the same lock, so a confirm and a rollback can never
both win.

### 5.4 Rollback, and how its result gets home

**Rollback (helper):** `rolling_back`,
`InstallFile(prev, prev_sha256, target)`, then restart the service and write
`rolled_back` with a reason (`no_confirmation` or `crash_loop`). If the
install fails, it retries three times, then writes `rollback_failed` and
restarts the service anyway. The endpoint then needs manual repair, and the
server sees the job expire as `lost` (§6.3).

**Reporting.** Once the old binary starts, `Resume` sees `rolled_back`,
`aborted` or `rollback_failed` with `reported: false`. It enqueues a `failed`
result with `outcome`, `reason` and both versions, then sets `reported: true`.
If the helper aborts before the swap took effect (for example, the self-test
fails), it still restarts the service so that this report is made.

**A reboot mid-update kills the helper.** At boot the service starts whichever
file sits at `target`, and that binary's `Resume` finishes the job. The new
binary, in `trial`, keeps its own deadline and start count. The old binary, in
`staged` or `swapping`, writes `aborted/interrupted` and reports. The swap is a
single rename, so there is no half state.

**The one residual.** If the new binary panics before `Resume` runs **and** a
reboot killed the helper during trial, nothing on the endpoint rolls back: the
service manager restarts a binary that cannot start until its start limit, and
the server sees `lost`. The pre-swap `--update-selftest` runs the same init
path, which makes this narrow but does not close it. It is why the pilot ring
exists.

### 5.5 Per-OS restart

| OS | Swap | Restart / rollback |
|---|---|---|
| **Linux (systemd)** | Rename-over while the old process runs (the kernel keeps its inode alive). | `systemctl reset-failed its-agent` (clears a `StartLimitBurst` trip), then `systemctl restart its-agent`. Rollback uses the same sequence with `prev`. `Restart=always` handles crashes during trial, and each start increments `starts`. |
| **macOS (launchd)** | Rename-over while running. | `launchctl kickstart -k system/com.toeshee.itsagent`. `KeepAlive` plus `ThrottleInterval 10` restarts crashes, which count toward `starts`. |
| **Windows (SCM)** | A running image cannot be replaced, so the helper stops the service first (`mgr.Service.Control(svc.Stop)` via `golang.org/x/sys/windows/svc/mgr`, then waits up to 60 s for `Stopped`), then does the rename-over. | `mgr.Service.Start()`. A clean stop does not trigger the SCM recovery actions. Crashes during trial do (`restart/5000/restart/15000/restart/60000` from `install.ps1`), and count toward `starts`. Downtime is the stop plus the start: seconds. |

---

## 6. Rollouts and rings (server)

Everything here is in the new package `backend/app/modules/agent_update/`
(`models.py`, `service.py`, `reconcile.py`, `api.py`, `schemas.py`,
`jobtypes.py`, `semver.py`). The new migration is `0083_agent_update.py`.

### 6.1 Channels

The migration adds `agent_releases.channel` (`pilot|stable`, default `pilot`),
set per version across all its `(os, arch)` rows by
`POST /api/v1/agent/releases/versions/{version}/promote`. That route requires
`agent.release.manage` (super-admin, as today) and is audited as
`agent.release_promoted`. A `pilot` release may be rolled out only to a pilot
ring. Every other ring requires `stable`. The platform operator decides
maturity. Each tenant decides timing.

### 6.2 Tables (org-scoped, RLS forced)

- **`agent_update_rollouts`**: `id`, `org_id`, `target_version`,
  `is_rollback` (target lower than the devices' current version; requires
  `agent.update.rollback`), `state` (`draft|running|paused|completed|cancelled`),
  `batch_size` (default 10, 1–100: targets in flight at once),
  `failure_threshold_pct` (default 5), `health_window_s`, `pause_reason`,
  `created_by`, `started_by`, `started_at`, `completed_at`. A partial unique
  index on `(org_id) WHERE state IN ('running','paused')` allows **one active
  rollout per org**.
- **`agent_update_rings`**: `id`, `org_id`, `rollout_id`, `ordinal`, `name`,
  `is_pilot` (ordinal 0 only), `device_group_id` (null means "every eligible
  device not in an earlier ring", allowed only for the last ring), `state`
  (`pending|active|done`), `activated_by`, `activated_at`. Ring 0 is required,
  must be `is_pilot`, and may hold **at most 25 devices**.
- **`agent_update_targets`**: `id`, `org_id`, `rollout_id`, `ring_id`,
  `device_id`, `release_id` (the row for the device's `(os, arch)`;
  `devices.os_family` `macos` maps to `darwin`), `from_version`, `job_id`,
  `state` (§6.3), `reason`, `attempts`, `next_attempt_at`, `updated_at`.
  `UNIQUE (rollout_id, device_id)`.

**Targets are materialized when the rollout starts**, from the preview the
human saw, and are not re-evaluated afterwards. A device enrolled mid-rollout
is not added.

### 6.3 Target states and what counts as a failure

| State | From | Counts toward the threshold |
|---|---|---|
| `pending` | initial; also after `busy` or `not_delivered` | no |
| `dispatched` | job created | no |
| `succeeded` | job `succeeded` **and** the post-update `hello` succeeded (§6.4) | no |
| `rolled_back` | `failed`, `outcome: rolled_back` | **yes** |
| `failed` | any other `failed`; `hello` failed or timed out (`degraded`) | **yes** |
| `refused` | refused with any token except those below | **yes** |
| `needs_manual_update` | refused `unknown_type`; or no reported version or no `agent.update` capability at preview | no (not dispatched) |
| `lost` | job expired after it was `dispatched`/`running` | **yes** |
| `not_delivered` → `pending` | job expired while still `queued` (never written to a socket) | no |
| `skipped` | no artifact for the platform; already at or above the target; ring closed while the device was offline | no |

`busy` and `update_in_progress` go back to `pending` with a 15-minute backoff,
up to 3 attempts, and then become `refused`.

**The reconciler derives every state from the `jobs` row.** It does not use a
result hook. That covers `succeeded`, `failed`, `refused` and `expired`
uniformly. Refusal tokens are read from the stored result.

### 6.4 The reconciler

The reconciler is the Celery beat task `agent_update.tick`, run every 60 s. It
returns immediately unless a rollout is `running`. For each running rollout:

1. Resolve the terminal jobs of `dispatched` targets into states (§6.3). For
   each job that just `succeeded`, dispatch a signed `hello` to the device, with
   a 10-minute TTL. That probe proves the **new binary verified and ran a
   signed job**, which confirming a connection alone does not. The target
   becomes `succeeded` only once the `hello` succeeds.
2. **Threshold.** Pause the rollout (`pause_reason = failure_threshold`) if the
   active ring's failures exceed `floor(failure_threshold_pct% × ring_size)`,
   **or** the last 3 resolved targets in the ring were all failures. In a pilot
   ring of fewer than 20 devices, the first failure pauses the rollout.
3. If the rollout is still running, top up to `batch_size` in-flight targets
   from `pending` targets whose device is **online** (`agents.status`) and has
   no `running` job. The job TTL is `1800 + health_window_s + 600` seconds:
   download and staging, the trial, and margin. The presigned URL is minted
   with that expiry plus 5 minutes.
4. When every target in the active ring has resolved, mark the ring `done`.
   **Rings are not auto-advanced** (§12, question 1). When the last ring is
   done, mark the rollout `completed`.

An operator can **close a ring** that still holds offline devices. Its
`pending` targets become `skipped`.

---

## 7. Rollback of a release

Local rollback (§5) handles a binary that does not come up. A release that
comes up but misbehaves is rolled back by a **rollback rollout**: an ordinary
rollout whose target is the previous release, flagged `is_rollback`, whose jobs
carry `rollback: true`. It requires `agent.update.rollout` **and**
`agent.update.rollback`, it must name the release it supersedes in its
`reason`, and it is audited as a rollback. Its pilot ring may be skipped
(ring 0 is not required), because it restores a version the fleet has already
run. It still dispatches in batches and still pauses on the threshold.
`DELETE /api/v1/agent/releases/{id}` returns 409 while any active rollout
references that release.

---

## 8. Server details

### 8.1 Job type registration

`register(JobType("agent.update", AgentUpdateParams, params_redactor=_redact_url,
cancellable=False))` goes in `agent_update/jobtypes.py`, called from
`register_module_job_types`. It is not cancellable: stopping a swap halfway is
worse than letting it finish or roll back. It has no `retry_relinker`, so the
kernel retry route refuses with a 403 that names the rollout routes.

### 8.2 Params construction

`service.build_params(target, release, rollout)` fills the params from the
`agent_releases` row: the server-computed `sha256`, `size_bytes`, and a
presigned GET from the existing `ObjectStore` presign path (`app.deploy`'s
`artifact_ref` uses the same one, and it is proven through Traefik on
`:443`: HANDOFF §2). `_redact_url` removes the query string wherever params are
returned or audited. The presign is a time-bounded read grant for one
fleet-identical object, not a credential, but there is no reason to spread it.

### 8.3 RBAC

Migration 0083 seeds the new codes:

| Code | Seeded to | Gates |
|---|---|---|
| `agent.update.read` | org-admin, technician | fleet version view, rollout list and detail |
| `agent.update.rollout` | org-admin | create, preview, start, pause, resume, advance ring, close ring, cancel |
| `agent.update.rollback` | org-admin | additionally required for an `is_rollback` rollout |

These codes are not in `PLATFORM_OPERATOR_CODES`: they act on one org's
devices. `agent.release.manage` stays super-admin only and also governs
promotion. The existing test that every code gates a live route covers all
three.

Routes, all under `/api/v1/agent`:

- `agent.update.read`: `GET fleet-versions`, `GET update-rollouts`,
  `GET update-rollouts/{id}`, `GET update-rollouts/{id}/targets`.
- `agent.update.rollout`: `POST update-rollouts/preview` (writes nothing),
  `POST update-rollouts` (creates a draft), and `POST update-rollouts/{id}/`
  `start`, `pause`, `resume`, `advance`, `rings/{ring_id}/close`, `cancel`.
- `agent.release.manage`: `POST releases/versions/{version}/promote`.

Cancel stops new dispatches. Jobs in flight finish or roll back on their own.

### 8.4 Audit events

All go to the hash-chained log, org-scoped:

- `agent.update.rollout_created`, `_started`, `_paused` (actor: a user, or
  `system` with `pause_reason`), `_resumed`, `_ring_advanced`, `_ring_closed`,
  `_cancelled`, `_completed`. Metadata: version, `is_rollback`, ring sizes,
  thresholds.
- `agent.update.target_resolved`: device, from and to versions, state, reason.
- The existing `jobs.*` events for each dispatch. For jobs the reconciler
  creates, the audit metadata carries `on_behalf_of`: the human who started or
  last advanced the ring.
- `agent.version_changed`: written by the gateway whenever a connect reports a
  version different from the stored one. The actor is the agent. This records
  every version change, including manual reinstalls.
- `agent.release_promoted`, which is platform-scoped.

### 8.5 Never automated, never AI

- `NON_APPROVABLE_REASONS["agent.update"]`: no approved action and no AI
  proposal can name it.
- `MODULE_GOVERNED_REASONS["agent.update"]`: the kernel `POST /jobs` route
  refuses it and names the rollout route.
- `DISPATCH_PERMISSIONS["agent.update"] = "agent.update.rollout"`.

The three allowlist partition tests break until all three entries exist. **No
scheduled or automated path creates a rollout or starts a ring.** The
reconciler only continues a ring a human started, and every ring advance is a
human action (question 1 in §12 asks whether that should change).

> **Amended by CLAUDE.md invariant 1c (2026-10-09):** an AI connected over the
> platform's MCP server acts as the person who connected it and may reach this
> act within that person's RBAC; the reservation above now governs automated
> and in-app-AI actors only. The approved-actions refusals
> (`NON_APPROVABLE_REASONS`) are unchanged.

### 8.6 Console (`frontend/src/modules/agent-releases/`)

- **Agent Packages → Fleet versions:** device counts per version, and a **Needs
  manual update** group (version unknown or no `agent.update` capability) that
  links to the install instructions.
- **Agent Packages → Rollouts:** a list view, and a creation wizard (version
  with its channel → pilot group → later rings → a preview of eligible,
  needs-manual-update, no-artifact and already-current counts per ring →
  start). The detail view shows ring progress, the per-device state and reason table,
  and Pause / Resume / Advance / Close ring / Cancel. Buttons render from
  `/me` permissions only as a convenience; the server enforces them.
- **Devices list and detail:** an "Agent version" column with a "Needs manual
  update" or "Update available" badge.

---

## 9. Idle budget

- **Registration:** a map insert. Nothing in `selfupdate` or `update` runs
  until a job arrives or `Resume` is called.
- **`Resume` with no state file:** one `os.Stat`, no goroutine, no timer.
  `agent/internal/update/budget_test.go` asserts that through the same
  `testing.Benchmark`-inside-a-`Test` pattern as the other budget tests: zero
  goroutines, zero timers, and fewer than 512 B/op.
- **Trial:** one `time.AfterFunc` and one confirmation hook, removed on
  confirm or rollback. That is minutes, once per update.
- **Headers:** computed once at start from `main.version` and
  `capabilities.Names()`.
- **Helper:** exists only between the handoff and the confirm or rollback,
  then exits. §11 asserts that no helper process and no transient
  unit/launchd job remains afterwards.
- **After the update:** the new binary is the agent. Its idle footprint is
  measured by the existing per-capability budget tests of the commit it was
  built from.

---

## 10. Code signature verification (Windows Authenticode, later macOS Developer ID)

It is not built until `authenticode-signing.md` is carried out. The seam is
defined now so that turning it on changes a constant and nothing else.

- **The pin is compiled into the running binary, never taken from params.** It
  is set with `-ldflags "-X .../update.windowsSignerPin=<value>"` by the
  release workflow. The value holds the expected publisher subject (O and CN)
  and the SHA-256 of the SubjectPublicKeyInfo of the acceptable issuing roots
  or intermediates. For option A's daily-rotated leaves, the pin is the
  subject plus the issuing chain, not the leaf certificate. A pin supplied by
  the control plane would be the control plane vouching for itself, which is
  the thing a signature exists to avoid.
- **Verification** happens at step 4 of §4.2, and again inside `InstallFile`
  on `.its-agent.new`. It calls `WinVerifyTrust` with
  `WINTRUST_ACTION_GENERIC_VERIFY_V2` through `golang.org/x/sys/windows`
  (no cgo), then extracts the signer chain with `WTHelperProvDataFromStateData`
  and compares it against the pin. It requires a timestamp countersignature.
  Revocation is checked with `WTD_CACHE_ONLY_URL_RETRIEVAL`: an unreachable
  CRL does not refuse the update, because endpoints are often offline, but the
  result reports `signature: verified` with `revocation: unchecked`.
- **Enforcement follows the pin.** If the running binary has an empty pin, the
  check is hash-only and the result reports `signature: not_required`. If the
  pin is non-empty, an unsigned or wrongly signed artifact is refused with
  `signature_required` or `signature_invalid` before staging. The first signed
  release is therefore installed by a hash-only update. **Every update after
  it requires the signature, including rollbacks**, so a rollback to a
  pre-signing release is refused. The server pre-filters those devices as
  `skipped` using `agent_releases.signed`, but the agent is authoritative.
- **Pin rotation** (a new publisher identity) ships as a release that carries
  the new pin and is signed under the old one.
- **The SHA-256 is still required and still checked first.** It is taken over
  the *signed* PE (`authenticode-signing.md` §3), so the two checks agree.
- **macOS, once a Developer ID exists:** the same seam, using
  `/usr/bin/codesign --verify --strict -R '=anchor apple generic and certificate leaf[subject.OU] = "<TEAMID>"'`
  on the staged file, with the TEAMID compiled in. **Linux** has no binary
  signature, so it stays hash-only (§12, question 2).

**What this buys, stated exactly.** With a pin, a compromised artifact store
or a mis-published release cannot put a foreign binary on Windows endpoints
through *this path*. It does not stop a compromised control plane, which still
holds `script.run`. The pin makes this path no weaker than the endpoint's own
trust. It does not make the control plane less trusted overall.

---

## 11. Definition of done

**Go tests, in the existing `agent` CI job:** the §4.1 refusal order, one test
per token, each asserting through a counting fetcher that no network call was
made; the §4.3 table; a hash, size or signature mismatch leaves no
`staged/its-agent`, no `state.json` and no helper; `InstallFile` re-hashes at
the target and refuses a file swapped after staging; every §5.3 transition,
driven through `Resume` with a fake clock, including the decision-lock race;
`ErrDeferred` from any type except `agent.update` becomes `failed`; the budget
test (§9); the semver vectors.

**Backend tests, against real Postgres:** the reconciler's §6.3 mapping from
`jobs` rows; the threshold pause, including the three-in-a-row rule and the
pilot first-failure rule; `not_delivered` is not a failure; dispatch goes only
to online devices; ring 0 holds at most 25 devices and non-pilot rings require
`stable`; one active rollout per org; a 403 for each code on each route;
`is_rollback` needs both codes; the three allowlist partitions; the gateway
header parser, including an invalid capabilities header being ignored; the
redactor strips the query string from API responses and audit metadata; the
semver vectors.

**End-to-end on the real OS: a new CI job `agent-update-e2e`.** It runs a
matrix of `ubuntu-latest`, `windows-latest` and `macos-latest`, on every push
to `main` and on every PR that touches `agent/**`,
`docs/kernel/wire-protocol/**` or `backend/app/modules/agent_update/**`.

**Setup.** Build four binaries from the commit: **A** `0.0.1-e2e.1`; **B**
`0.0.1-e2e.2`; **C** `0.0.1-e2e.3` with the build tag `e2e_noconfirm` (never
dials); **D** `0.0.1-e2e.4` with the tag `e2e_badselftest` (its
`--update-selftest` exits 1). Install **A as a real service** with the shipped
`install.sh` or `install.ps1`, which on Windows and macOS is also the first
execution those scripts have ever had, and pre-seed its data directory with a
test identity. The harness `agent/internal/update/e2e/` is a small mTLS
WebSocket gateway plus an HTTPS artifact server; it signs `agent.update` jobs
with the test job key, and its test CA goes into the runner's system trust
store (`update-ca-certificates`, `security add-trusted-cert`,
`Import-Certificate Cert:\LocalMachine\Root`).

The job must observe each of these on each OS:

1. **A→B** succeeds: the service runs B from the same path, the upgrade
   header reports B, the deferred `succeeded` result arrives, `prev/` holds A,
   and no helper process, transient unit or launchd job remains.
2. **B→C** rolls back within `health_window_s = 120`: the service runs B
   again, and the result is `failed / rolled_back / no_confirmation`.
3. **B→D** aborts before the swap with `selftest_failed`, and B is still what
   runs.
4. **A tampered artifact** fails with `hash_mismatch`: nothing appears in the
   target directory, and the service was never stopped.
5. **B→A** without `rollback` is refused with `downgrade_not_permitted`. With
   `rollback: true`, it succeeds.
6. **Kill the helper during C's trial**, which simulates a reboot. The new
   binary's own deadline triggers the rollback, and the old binary reports it.
7. **Throughout scenarios 1–6**, a sampler records the listening sockets owned
   by any `its-agent` process and finds none. The tools are `ss -ltnp` on
   Linux, `lsof -iTCP -sTCP:LISTEN` on macOS and `Get-NetTCPConnection -State
   Listen` on Windows.

Until this job is green on all three runners, the release notes and
`first-run-verification.md` say "self-update: never executed on
Windows/macOS", in the same way `spec.md` §3 marks the installers.

**Deploy note:** the only server change is migration 0083. It is forward-only
and adds tables, one column on `agents`, one on `agent_releases`, and
permission seeds. No new `.env` keys.

---

## 12. Decisions still open for the operator

1. **How do rings advance?** (a) always by a human, as specified; (b)
   automatically after a soak period, for example 24 h with no failures; (c)
   pilot to the next ring by a human, later rings automatically.
2. **Add an offline release-signing key now?** An Ed25519 key held by the
   release workflow, with the public half compiled into the agent, would give
   Linux and macOS the "key the control plane does not hold" property without
   procurement. (a) no, hash-only as approved; (b) yes, in this cycle; (c)
   together with the release workflow (`spec.md` §8.1).
3. **Who may start a rollout?** (a) org-admin, for releases a super-admin has
   promoted to `stable`, as specified; (b) super-admin only; (c) org-admin for
   any published release.
4. **The first hop for today's fleet**, which cannot self-update (§3.4). (a)
   manual reinstall or GPO/Intune, as specified; (b) a documented one-off
   `script.run` that installs the first update-capable release detached from
   the job, with no rollback.

---

## 13. Rejected alternatives

- **In-process `exec` of the new binary.** There is no watchdog, a binary
  that cannot start cannot roll itself back, and Windows cannot replace a
  running image anyway.
- **Repointing the service definition at versioned directories** (ImagePath or
  a systemd drop-in). It breaks path-pinned TCC grants and path-keyed firewall
  or AV rules, and it fights dpkg (§5.1).
- **Package-manager updates** (a signed apt repository, MSI, winget). None of
  those exist yet, rollback differs on every OS, and there is no MSI at all.
  They stay the manual path.
- **The agent polling for new versions.** That breaks the rule of one parked
  connection with no polling loop. The control plane decides.
- **A permanent watchdog service.** A second always-on process breaks the idle
  budget. The helper exists only during an update.
- **Rollback ordered by the server.** An agent that cannot connect cannot be
  told to roll back, so the local gate is what makes rollback work.
- **Helper IPC over a local socket or named pipe.** That would be a listening
  endpoint. Files in a root-only directory are enough.
- **A new `JobResultMsg.state` such as `rolled_back`.** That would be fatal
  from a newer agent to an older control plane. `failed` plus `outcome`
  carries the same information.
- **Reporting version in the heartbeat.** Covered in §3.3.
- **Keeping the refusal.** It left the fleet with no upgrade path and bought
  no protection that `script.run` does not already give away (§1).

## Operator decisions (2026-09-23)

| Question | Decision |
|---|---|
| Signed self-update at all | **Yes**: signed job, SHA-256 pinned, rollback, rings. |
| Ring advance | A person advances every ring. |
| Offline release-signing key | Added together with the release workflow: an Ed25519 key held by the release workflow and compiled into the agent, checked in addition to the SHA-256. |
| Who starts a rollout | An org-admin, for releases a super-admin has promoted to stable. |
| First hop for agents already deployed | Manual reinstall (the real fleet is not enrolled yet). |
| Windows Authenticode | Unsigned until the pilot (see the remote-control decisions); once signing exists, updates require the signature. |

## Implementation notes (2026-09-23)

Where the build refined the design above. Each is small; none changes an
invariant.

- **Migration `0095_agent_update.py`**, not `0083` (that number was taken
  first). It also adds `agent_releases.release_signature`. No new `.env` keys.
- **The offline release key (operator decision above).** `AgentUpdateParams`
  gains the optional `release_signature`: base64 Ed25519 over
  `its-agent-release/v1\n<version>\n<os>\n<arch>\n<sha256>\n`. The public half
  is compiled in (`internal/update.releaseSigningKey`, set by the release
  workflow from the secret `AGENT_RELEASE_SIGNING_KEY`). It is checked at a new
  step **3a**, straight after both versions parse and before ordering, so
  nothing is reasoned about a release whose provenance fails. Refusals:
  `signature_required`, `signature_invalid`; the result's `signature` reports
  `verified` or `not_required` for this check. The Authenticode seam (§10)
  exists as `windowsSignerPin`; a build that sets it refuses every update until
  the verification is written, rather than silently skipping it.
- **Tokens added to §4.4:** `bad_params` (step 1), `unbound` (the capability
  was never bound), `staging_failed` (the data directory could not be written),
  `service_control_failed` (Windows could not stop the service for the swap).
  Handler failures before the handoff arrive as `failed` with
  `{"error": <token>}`; deferred outcomes as `{"outcome", "reason", ...}`.
- **`InstallFile` is `Prepare` then `Commit`.** On Windows the helper copies,
  re-hashes and self-tests the candidate *before* stopping the service, so a
  bad candidate costs no downtime; the rollback path stops the service on every
  attempt, because SCM recovery may have restarted the failing binary.
- **Helper liveness is an OS lock** (`<data>/update/helper.lock`, `flock` /
  `LockFileEx`), which the kernel drops when the helper dies, reboot included.
  A starting binary uses it to tell "a helper is mid-update, leave it" from "a
  reboot killed it, finish the job".
- **Order of writes around a restart.** The helper writes `trial` (with its
  deadline) *before* restarting onto the new binary, and `rolled_back` *before*
  restarting onto the old one, so whichever binary starts next reads the state
  it needs. A new binary that decides to roll back while the apply helper is
  alive only records `rolling_back`; the live helper carries it out.
- **Exit code 7** is a binary stepping aside for a rollback.
- **A `verifying` target state** covers the post-update `hello` (§6.4), with
  `probe_job_id` on the target. A job still `running` past its `expires_at` is
  `lost`, because the kernel expires only queued and dispatched jobs.
- **Routes added:** `PUT /api/v1/agent/releases/{id}/release-signature`
  (`agent.release.manage`). `ReleaseOut` gains `channel` and `release_signed`.
  `DELETE /agent/releases/{id}` checks every org's active rollouts through the
  admin engine, since RLS would hide them from the caller's session.
- **Console.** The fleet-version view, "Needs manual update" and rollouts live
  on Agent Packages. The Devices list column (§8.6, third bullet) is not built.
- **`agent-update-e2e` has its own workflow file,** because the path filter in
  §11 cannot be expressed for one job inside `ci.yml`. It first runs a smoke
  test without root or a service (the harness, and the pre-download checks on a
  real binary with a compiled-in key), then the six scenarios as root.
