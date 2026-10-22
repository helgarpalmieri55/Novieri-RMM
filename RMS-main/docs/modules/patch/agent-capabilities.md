# `patch.scan` / `patch.apply` — the agent half

This is the endpoint side of `docs/modules/patch/spec.md` §10, implemented in
`agent/internal/capabilities/patch/`. The spec's PD-01 scoped the last cycle to
"full server slice, agent engines mocked"; this is the cycle that removes the
mock. Read §10 first — it defines the contracts. This document records the
decisions the spec left to the endpoint, and, in §6, what has never run.

---

## 1. Antivirus is a design constraint, not an afterthought

The fleet is Windows 11 with Defender and, on most sites, a third-party EDR.
`patch.apply` runs as SYSTEM. That combination rules out the obvious
implementation.

**Windows uses the Windows Update Agent COM API, in process.** No child process
at all. Not `powershell.exe`, not `UsoClient.exe`, not `wusa.exe`.

| Option | Why not |
|---|---|
| `powershell.exe -Command "…"` | A SYSTEM process spawning PowerShell is among the loudest behavioural signals an endpoint emits. It is what Defender's ASR rules and most EDR behavioural chains are built to catch. An RMM agent that trips those on every patch window is one the security team uninstalls. |
| `UsoClient.exe` | Fire-and-forget. It returns immediately and reports no per-update outcome, so an agent built on it would have to **invent** the `PatchApplyResult.results` the wire contract asks for. |
| `wusa.exe` | Installs a `.msu` file this agent does not have and has no business downloading itself. |
| **WUA COM** | Enumerates *and* reports. Spawns nothing, writes no script to disk, and reads to an EDR as ordinary use of a documented API — it is what Windows Update itself does. |

macOS and Linux do shell out, to the narrowest binary that can do the work
(`/usr/sbin/softwareupdate`, `apt-get`), always with a fixed argv and never
through a shell.

### Nothing job-supplied ever reaches an argument, and that is structural

`engine.Install` takes an `entry`, and an `entry` is only ever produced by
`engine.Available` — that is, by the OS describing itself. There is no
`Install(identifier string)`. A signed job's `{source, identifier}` pair is used
as a **map key** and nothing else: a string-equality test against what the OS
already offered.

So a job asking for `coreutils; rm -rf /` reaches no installer call at all —
not a sanitised one, not an escaped one, none. `patch_test.go`'s
`TestNothingJobSuppliedEverReachesTheEngine` asserts that absence over seven
hostile shapes, paired with a control case that *does* install, without which
the absence would pass on an engine that installs nothing.

The subtler variant is asserted too: the job supplies only the KEY, so title,
severity, kb and cve must come from the OS. An implementation that built an
entry from the job's map would pass the injection test and still let a signed
job dictate what the installer saw.

---

## 2. Per-OS coverage, and the gaps

| OS | Source | Scan | Apply | Executed here? |
|---|---|---|---|---|
| Windows | `windows_update` | WUA COM, one search | WUA COM download + install | **No** — see §6 |
| macOS | `softwareupdate` | `softwareupdate --list` | `softwareupdate --install <label> --no-restart` | **No** — see §6 |
| Linux | `apt` | `apt-get -s upgrade` | `apt-get --only-upgrade install pkg=version` | **Yes** |
| Windows apps | `winget` | `winget upgrade` (table) | `winget upgrade --id <id> --exact --silent` | **No** — fixture reconstructed |
| macOS apps | `homebrew` | `brew outdated --cask --json=v2` | `brew upgrade --cask <token>` | **No** |

**App sources (2026-09-23).** winget and Homebrew casks list outdated
applications alongside the OS engine (`apps.go`). Their entries use the
package id (`Google.Chrome`, `firefox`) as the identifier, so one approval
covers later versions of the same app; the server moves an `installed` app
back to `missing` when a newer version is offered. Each scan reports
`app_sources`: `scanned`, `unsupported` (with the reason) or `failed`, and a
failed app source never costs the OS scan. App entries and `app_sources` are
sent only under `result_version` 2.

- **winget** is found by `deploy.WingetPath` (the App Installer package
  directory, because LocalSystem has no WindowsApps alias). As SYSTEM it sees
  machine-wide installs only; per-user installs are not claimed. The table is
  read from the right because names hold spaces and wide characters; a
  truncated id is skipped, and the "explicit targeting" table after it is
  never read. `0x8A150109`/`0x8A15010A` count as installed with a restart due.
- **Homebrew** refuses to run as root, so brew runs as the account that owns
  the prefix, with a fixed environment and that account's home. A root-owned
  prefix, or no brew, is `unsupported`. Casks only, and only the ones brew
  calls outdated (self-updating casks are left to themselves). A cask that
  installs a `.pkg` asks for sudo and fails with brew's own message.

**Not implemented, deliberately:** `dnf`, `zypper`. Spec §10 names them. A host with none of the implemented engines **refuses** rather
than failing — `jobs.Refuse`, because a host with no supported package manager
will not grow one by being retried, and the control plane should hear "this
agent will not do this" once rather than re-dispatch every night forever.

`dnf` in particular was left out rather than guessed at. A parser written
against output nobody here has captured is a fixture-shaped guess wearing the
clothes of coverage; the apt capture in §5 shows exactly what invented fixtures
miss.

---

## 3. `installed_identifiers` is small on purpose

`PatchScanDocument.installed_identifiers` exists for reconciliation. This build
populates it on **Windows only**, and that is a cost decision about the control
plane rather than an omission.

`ingest.py` resolves every installed identifier that is not also in `patches`
with **its own SELECT** (the `unresolved_keys` loop). So:

- **Windows** — the installed half of the same WUA search: tens of KB articles.
  Reported, capped at 512.
- **apt** — the equivalent set is the entire dpkg database, 500–2000 packages.
  Reporting it would cost the control plane one query per package per device per
  scan; on a thousand-device fleet scanning daily that is millions of queries to
  learn almost nothing. **Empty.**
- **macOS** — `softwareupdate --history` is a fixed-width table of display names
  with no relationship to the `--list` labels this engine uses as identifiers.
  Its identifiers would match nothing in the catalog, so every one would be a
  wasted SELECT. **Empty.**

**The cost of the omission, stated:** a patch the catalog has seen before, which
some *other* mechanism (unattended-upgrades, a human at a terminal) installed
between two scans, reconciles to `not_applicable` instead of `installed`. Both
mean "not outstanding on this device", so the compliance percentage is
unaffected; the audit trail is slightly less precise.

---

## 4. Reboot: honoured under `result_version` 2

Until 2026-09-23 this section recorded a gap: `PatchApplyResult` had one
reboot member, so a restart would have been invisible to the control plane and
the agent only reported `reboot_required`. The wire now carries
`reboot_scheduled_for` and `user_present`, and the control plane announces
that it reads them with `result_version` 2 (the wire-protocol README says why
it must be announced). `reboot.go` holds the decision:

- `never` never restarts; `if_required` restarts when the OS says so;
  `forced` always does.
- A signed-in user on a workstation gets `deferral_hours` of notice, clamped
  to 15 minutes .. one week. Everyone else gets five minutes, and nothing is
  scheduled when that would land after `reboot_not_after`: the control plane
  then books it in the next window with `patch.reboot`.
- The warning is the OS's own restart notification. The message is this
  agent's text; only numbers it computed reach the command line.
- A failed restart does not fail the installs.

Under an older control plane the agent emits the v1 documents and never
restarts. `reboot` is still REFUSED when it is not one of the three known
values, for the reason below.

**Never executed:** `restart_windows.go` (WTSEnumerateSessions,
RtlGetVersion's ProductType, `shutdown.exe`), and `shutdown -r +N` on macOS
(whether it forks and returns, and whether its warning reaches a GUI user: it
does not appear to, which is the gap a user-session helper closes). Linux uses
systemd's `shutdown`, which hands the schedule to logind.

`reboot` is REFUSED when it is not one of the three known values. Not
defaulted: it is the operator's stated intent for a privileged, irreversible
action on a machine somebody is using. A fourth value added server-side takes
every deployed agent's patch apply offline until the fleet updates, and
`schema_test.go` pins the enum so that change cannot be made silently.

---

## 5. Severity, per source

`PatchEntry.severity` is a `Literal` server-side and its `ValidationError` is
raised on the **whole document**, so one invented severity costs a device its
entire scan. Every mapping here therefore falls back to `unknown` rather than
passing a value through.

- **Windows** — `IUpdate.MsrcSeverity`, mapped by an explicit switch (never
  `strings.ToLower`: the two vocabularies happen to share four words, which is
  the coincidence that makes a passthrough look correct until Microsoft returns
  a fifth). **An unrated update is `unknown`, not `low`** — most Windows 11
  cumulative updates carry no MSRC severity despite containing security fixes,
  and calling them `low` would tell the compliance report something Microsoft
  did not say.
- **apt** — Debian and Ubuntu publish **no** per-package severity. What they
  publish is the archive a version came from. A version from a `-security`
  pocket is by definition a security update → `important`; everything else →
  `unknown`. `important` and not `critical` because `critical` is a judgement
  about exploitability the archive does not make, and PD-02 treats both the same
  way.
- **macOS** — `softwareupdate --list` publishes no severity either. It publishes
  `Recommended: YES|NO`. `YES` → `important` is a **judgement**, and it is worth
  naming because PD-02 auto-approves `critical|important`: on an org with
  `auto_approve_security` on, every Apple-recommended update auto-approves into
  the `test` ring. That is the intent of the rule — the test ring is where a
  security update is supposed to land first — and `moderate` would instead make
  the feature a no-op on half the fleet.

Sentinel's `priority` column (spec §4) is the seam where a real severity feed
lands later. Nothing here writes it.

### Identifiers

- **Windows** — the lowest KB article (`KB5034123`), falling back to the
  `IUpdateIdentity.UpdateID` GUID for updates with no KB. *Lowest*, not first:
  an identifier that varied with WUA's enumeration order would produce a second
  catalog row and a device that looks permanently unpatched.
- **apt** — `pkg@version`, which is spec §4's stated form and what makes
  reconciliation work: once installed, that exact string stops being offered.
- **macOS** — the `--list` label verbatim, because it is the only string
  `--install` accepts.

`superseded_by` is always `null`. WUA exposes `SupersededUpdateIDs` — what an
update *replaces*, the opposite direction — and no property for what replaces
it. Guessing the reverse edge could point a catalog row at the wrong patch, and
`ingest.py` treats an unresolvable value as absent anyway.

---

## 6. What has never executed — read this before trusting the green suite

`agent/internal/capabilities/patch` has a passing test suite on Linux. That
suite does **not** cover the following, and no test in this repository can.

### Never run anywhere

1. **`com_windows.go` and `wua_windows.go` — the entire Windows engine.** The
   COM apartment, `CoCreateInstance`, every `IDispatch::Invoke`, the VARIANT
   marshalling, the BSTR lifetimes, the download/install loop and the
   `OperationResultCode` handling. There is no Windows runner and no Windows
   Update Agent to run it against. What they have is `go vet` and `go build`
   with `GOOS=windows`, which type-check them — a real check (a wrong method
   *signature* fails to compile) and a small one (a wrong DISPPARAMS argument
   *order* compiles perfectly and does something else).
2. **`macEngine`'s shell-outs.** `softwareupdate --list` and
   `softwareupdate --install` have never run. The *parser* is tested, but see
   the next item.
3. **The macOS fixture is RECONSTRUCTED, not captured.**
   `testdata/softwareupdate-list-RECONSTRUCTED.txt` was written by hand from the
   documented format. No Mac was involved. If real output differs, the tests
   over it stay green and **every Mac in the estate reports as fully patched**,
   which is the most dangerous wrong answer this capability can give. See
   `testdata/README-fixtures.md` for exactly what to do about it.
4. **macOS updates that need volume-owner credentials.** A macOS OS update on
   Apple Silicon requires `--user`/`--stdinpass`. This agent has no stored
   credentials to offer and deliberately does not grow a path that would —
   invariant #3 puts credentials in the vault, decrypted at point of use, which
   is a server-side facility with no agent-side equivalent today. Such an update
   fails with Apple's own authorization message in `last_error`.
5. **`engine_other.go`.** No cross-compile target reaches it.

### Run here, and what that covers

- **`apt` end to end**, against real `apt-get` on the CI container
  (`TestTheRealAptEngineDescribesThisHost`), plus a **verbatim capture** —
  `testdata/apt-get-s-upgrade-real.txt`, 372 lines, 166 `Inst` lines, 136 from
  a `-security` pocket. That capture earned its place: it contains two shapes
  the parser's author did not know about until it was taken (comma-separated
  multi-origin groups, and a trailing `[]` after the closing parenthesis). An
  invented fixture would have had exactly the shape the parser already handled.
- **Everything on the near side of the engine seam**: the wire structs against
  the exported schemas, the scan document's construction and clamping, the
  apply matching and outcome mapping, refusal-vs-failure classification, error
  sanitisation and the idle budget.

### Structural limitations worth knowing

- **The job's context cannot interrupt a COM call.** WUA calls are synchronous
  and take no cancellation token; the deadline is honoured *between* updates,
  not inside a call. The alternative — running the search on another goroutine
  and abandoning it — would leave a COM apartment and its interface pointers
  alive with nothing owning them, which is exactly the resident cost the idle
  budget forbids.
- **No `job_log` progress streaming.** Spec §10 says `patch.apply` should
  "stream progress (`job_log`)". `jobs.Handler` is
  `func(context.Context, VerifiedJob) (any, error)` — a capability has no
  `Sender`. Giving it one is a kernel change to `internal/jobs`, which this
  module registers into and does not fork (CLAUDE.md build discipline §3). The
  result document is complete; only the narration is missing.

---

## 7. Idle budget — measured

CLAUDE.md names patch **first** in its list of heavy capabilities that must stay
dormant. `budget_test.go` measures four falsifiers through `internal/budget`, in
ordinary `Test` functions that COMPARE — never `Benchmark` functions, because
`go test -bench` exits 0 on a regression.

Measured on the development container, no `-race`:

| Falsifier | Measured | Threshold |
|---|---|---|
| 1 — dormant path allocates | 0 allocs/op, 0 B/op, 0 goroutines over 549,783,145 iterations | 0 |
| 1 — timer-wheel entry | unchanged across a scan and an apply | 0 |
| 1 — file descriptors | 6 before, 6 after 50 scans + 50 applies | no growth |
| 2 — RSS after one scan + one apply | **−266,240 bytes** (it shrank) | 4 KiB |
| 2 — RSS after 20,000 pairs | +778,240 bytes | 4 MiB |
| 3 — retained heap, 100 vs 10,000 pairs | 96 B vs 288 B, delta **192 B** | 16 KiB |
| 4 — one 20-entry scan | 122 allocs/op, 14,608 B/op | 32 KiB |
| 4 — one 5-patch apply | 180 allocs/op, 18,536 B/op | 32 KiB |

Every threshold was verified by planting a regression. One attempt is recorded
in the test file because it **failed to prove anything**: a bare
`_ = make([]byte, 64<<10)` is eliminated as dead code and the mutation survived.
Re-planted so it escapes, the same check read 96,528 B/op and went red.

**`~0% CPU at idle` is NOT asserted** as a wall-clock number, and that is a
deliberate refusal rather than an oversight — the same one `internal/budget`'s
package comment makes. A shared runner's variance exceeds the signal; a
threshold loose enough not to flake is loose enough to pass a real regression,
and a flaky budget test gets skipped, which reads as coverage while measuring
nothing. It remains a **design target**, approached through the four
machine-independent proxies above.

---

## 8. Registration

The capability exports in the same shape as the others. The two lines belong in
`agent/internal/capabilities/registry.go`'s `mustBuild`:

```go
must(r.Register(patch.JobTypeScan, patch.Process.Scan))
must(r.Register(patch.JobTypeApply, patch.Process.Apply))
```

Registration is a map insert. `patch.Process` is a zero-value `Service` that
resolves its engine at **job time**, so nothing opens a file, dials anything,
starts a goroutine or takes a timer-wheel entry until a verified job arrives.

Both are registered **unconditionally**, like the rest. An agent that advertised
`patch.scan` only on a host where it found a package manager would be an agent
whose patch state silently stops arriving; whether this device should be patched
is the control plane's decision, and a host with no engine answers `refused`
once, which is a fact the control plane can act on.

There is **no local schedule**. The scan cadence is server-side
(`scan_interval_hours`, spec §12's beat) for the same reason `net.sweep`'s and
`site.wake`'s are: a locally scheduled patch apply is a standing authorization,
signed once and replayed for weeks after the approval was revoked.
