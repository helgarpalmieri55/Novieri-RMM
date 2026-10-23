# `script.run` and `app.deploy` — the agent side

> Companion to `docs/modules/app-deploy/spec.md`. The spec is the module; this
> is the endpoint half of it: what the Go agent actually does when one of these
> two signed jobs arrives, what it refuses, what it spawns, what it measured,
> and — the part that matters most for a capability that runs code as
> SYSTEM/root — **what it has never executed**.
>
> Code: `agent/internal/capabilities/deploy/`. Date: 2026-08-06.

---

## 0. Why one Go package for two job types

`script.run` and `app.deploy` share the whole download / verify / execute
machinery: the bounded process spawn, the output cap, the exit-code mapping,
the restricted scratch directory, the argv planner. Go has no way to share
unexported machinery across packages, and exporting it would mean the seam that
keeps job-supplied strings off a command line is a public API anyone can route
around.

`internal/capabilities/cert` already registers two job types (`agent.cert.renew`
and `agent.cert.install`) from one package for the same reason, so this is the
established shape here and not a new one.

**Registry entries** (`agent/internal/capabilities/registry.go`, in
`mustBuild`):

```go
must(r.Register(deploy.JobTypeScript, deploy.Process.Run))
must(r.Register(deploy.JobTypeDeploy, deploy.Process.Deploy))
```

`deploy.Process` is a zero-value `*Service` that resolves every default — GOOS,
`exec.LookPath`, the executor, the fetcher, the scratch root — at **job time**.
Registration is a map insert and nothing else; §5 measures that rather than
claiming it.

---

## 1. The security shape

### 1.1 The signature is the only authorization

Both handlers take a `jobs.VerifiedJob`, which is an interface with an
unexported method, so this package **cannot be handed anything that has not
been verified** (`agent/internal/jobs/verified.go`). By the time either handler
runs, `internal/jobs` has checked the Ed25519 signature over the canonical
bytes against the key pinned at enrollment, that the job targets this device,
that it is fresh, that its nonce is unseen, and that the type is registered.

For `script.run` the signed params **contain the script body** (AD-02). There
is no local allowlist of permitted scripts, no cached approval, and no "we ran
this last week" path.

For `app.deploy` the signed params **contain the installer's SHA-256** (AD-07).

### 1.2 The download is not the authorization

Object storage is not a trust boundary — whoever can write the bucket can
change the bytes. What authorizes an installer to run as SYSTEM is the server's
signature over the params that *named its hash*.

`stage()` in `install.go` makes that structural rather than procedural:

1. bytes stream from the `Fetcher` into `download.part`, hashed on the way
   through. **No `Command` ever names that path.**
2. the computed digest is compared against the signed one
   (`subtle.ConstantTimeCompare`).
3. **only then** does `os.Rename` create `package.msi` / `package.exe` /
   `package.pkg` — the path the planner names.

So on a mismatch the file the installer would run *does not exist*. "The
installer did not run" is not something a caller has to remember to check.
`hash_test.go` asserts both halves: the executor spy recorded zero spawns, and
`os.Stat` on the executable path fails.

### 1.3 The partition is the control

Which branch an `app.deploy` job takes is a function of `installer_type`
**alone**, never of whether `artifact_ref` happens to be present:

| partition | `installer_type` | artifact | hash checked |
|---|---|---|---|
| artifact-backed | `msi` `exe` `pkg` `dmg` `deb` `rpm` `script` | required | **yes** |
| manager-sourced | `winget` `choco` | must be **absent** | n/a |

- an artifact-backed type with **no** `artifact_ref` or **no**
  `artifact_sha256` → **refused**, nothing downloaded;
- a manager-sourced type that **carries** `artifact_ref` or `artifact_sha256` →
  **refused**.

There is therefore no job shape that reaches a process spawn with either a
verified hash or no artifact at all. A branch selected by "is there an
artifact?" would turn a nulled-out `artifact_ref` into a way to reach an
execution path that does no hash verification; `schema_test.go` asserts the
partition covers the exported enum exactly, so a new `installer_type` on the
server turns this package red rather than silently landing in the wrong branch.

### 1.4 Never log a body, never log an argument

A script body and `install_args` carry credentials — that is what operators put
in them. The rule is the one `internal/jobs` already applies to refusal
reasons: the agent's own sentences only, nothing job-supplied echoed. Refusals
name the **position** of a bad argument, never its content.

The one thing that *is* returned is the executed process's **output**, because
that is the contract (`InstallOutcome.log`) and the operator asked for it. It
is capped, tail-kept and stripped to printable ASCII first.

### 1.5 Refused vs failed

`refused` asks to be taken out of rotation; `failed` asks to be retried.

| situation | answer |
|---|---|
| interpreter / package manager not installed on this device | `refused` |
| `installer_type` with no engine on this platform | `refused` |
| malformed params, bad digest shape, disallowed argument | `refused` |
| `artifact_ref` this agent cannot resolve (see §4) | `refused` |
| artifact download failed or was cut off | `failed` |
| the artifact's hash did not match | `failed` |
| the process could not be started | `failed` |
| the process ran and exited non-zero | **succeeded job, failed outcome** |

The last row is the one that is easy to get wrong. "The installer failed" and
"there was no installer" are different facts; folding them together is how a
missing binary comes to look like a failed install.

---

## 2. Antivirus and EDR

`script.run` is definitionally the behaviour EDR exists to catch: a
SYSTEM-level service spawning an interpreter. Three rules, enforced in code:

1. **Nothing is ever a command line.** Every process is spawned with an
   explicit argv through `exec.CommandContext` — never `sh -c`, never `cmd /c`.
   A source walker in `plan_test.go` fails the build if `"sh"`, `"cmd"`,
   `"cmd.exe"`, `"-c"`, `"/c"`, `"-Command"` or `"-EncodedCommand"` appears as
   a string literal in non-test code, or if `exec.Command` is called instead of
   `exec.CommandContext`.
2. **No `-EncodedCommand`, ever.** A base64 payload on a PowerShell command
   line is a top-tier malware indicator. A PowerShell body is written to a
   `.ps1` in a directory only SYSTEM and Administrators can open, and `-File`
   names that path.
3. **The spawn list is closed and documented** (§3), and
   `TestTheDocumentedSpawnListIsTheOnlyOneThePlannerCanProduce` drives every
   `(goos, interpreter)` and `(goos, installer_type, action)` combination
   through the planners and asserts nothing outside the list can be chosen —
   and that nothing *on* the list is unreachable, because a stale entry in an
   allowlist is a hole nobody is defending.

`pwsh.exe` is preferred over `powershell.exe` where present. That is not a
style preference: `pwsh.exe` is a distinct binary, so an EDR rule can tell a
managed script run from the Windows PowerShell that every piece of malware on
the platform also uses.

`-ExecutionPolicy Bypass` **is** passed. The body's authorization is the
Ed25519 signature the runner already checked, not an execution policy set by
whoever last touched the registry on that box. It applies to that invocation
only. The alternative is signing each `.ps1` with a code-signing certificate
the platform does not currently have — worth revisiting, and out of scope here.

---

## 3. The full list of processes this agent may spawn

Give this to the EDR team. Every name is a compile-time constant in
`plan.go`'s `spawnable` map, and the test named above asserts the list is
complete and has no dead entries. Parent process in every case is the agent
service, running as SYSTEM (Windows) or root (macOS).

| program | platform | job type | invocation |
|---|---|---|---|
| `pwsh.exe` | Windows | `script.run` | `-NoProfile -NonInteractive -NoLogo -ExecutionPolicy Bypass -File <scratch>\script.ps1` |
| `powershell.exe` | Windows | `script.run` | same, only when `pwsh.exe` is absent |
| `pwsh` | macOS | `script.run` | same |
| `bash` | macOS | `script.run` | `bash <scratch>/script.sh` |
| `python3` | Windows, macOS | `script.run` | `python3 <scratch>/script.py` |
| `python` | Windows, macOS | `script.run` | only when `python3` is absent |
| `msiexec.exe` | Windows | `app.deploy` | `/i\|/x <scratch>\package.msi /qn /norestart [install_args…]` |
| `winget.exe` | Windows | `app.deploy` | `install\|upgrade\|uninstall --id <pkg> --exact --silent --disable-interactivity [--accept-package-agreements --accept-source-agreements]` |
| `choco.exe` | Windows | `app.deploy` | `install\|upgrade\|uninstall <pkg> -y --no-progress --limit-output` |
| `installer` (`/usr/sbin/installer`) | macOS | `app.deploy` | `-pkg <scratch>/package.pkg -target /` |
| **the hash-verified `exe` artifact itself** | Windows | `app.deploy` | `<scratch>\package.exe [install_args…]` |

The last row is the one spawn whose program name is not a compile-time
constant. It is safe for exactly one reason: that path exists only because the
SHA-256 in the signed params matched the fetched bytes (§1.2).

`<scratch>` is a per-job directory created with `os.MkdirTemp` under the OS
temp directory, locked to SYSTEM + Administrators on Windows and mode `0700`
on POSIX, and removed when the job ends.

**Child processes are not this list.** An MSI bootstrapper spawns `msiexec`
children; `choco` spawns PowerShell; an installer spawns whatever it likes.
Those are the package's behaviour, not the agent's, and no allowlist here can
enumerate them.

---

## 4. Two real gaps in the wire contract

Both are surfaced as **loud refusals** with the missing thing named, rather
than worked around. A contract gap that is not named becomes a permanent
mystery in a dashboard.

### 4.1 `artifact_ref` is an `s3://` URI no agent can resolve

`ObjectStore.put_package_artifact` (`backend/app/core/storage.py`) returns
`s3://<bucket>/<key>`, and that value goes straight into `AppDeployParams`
(`app/modules/deploy/dispatch.py`). **There is no agent-facing route that
resolves one.** The agent's HTTP surface is enrollment plus the WebSocket
gateway; `app/api/v1` has no package-artifact endpoint (the only `artifact` in
`app/api/v1` and `app/gateway` is `artifact_ref` on a job-log message).

An agent cannot fetch an `s3://` URI without object-store credentials, and it
must never have them — invariant #3 keeps stored credentials in the vault,
decrypted at point of use, not distributed to every endpoint in the fleet.

**Consequence today:** every artifact-backed `app.deploy` is refused with a
reason naming the gap. Manager-sourced (`winget`/`choco`) deployments work.

**The server-side fix:** put a presigned `https://` URL, or an authenticated
agent-facing download route, in `artifact_ref`. The agent already handles
`https://` and needs no change. `http://`, `file://` and bare paths are
refused — a local path would turn `artifact_ref` into "execute this file
already on the box", which is a different capability wearing a deployment's
clothes.

### 4.2 `AppDeployParams` carries no package identifier

`winget` and `choco` need a package **identifier** on the command line.
`AppDeployParams` has `action`, `installer_type`, `artifact_ref`,
`artifact_sha256`, `install_args`, `detection_rule`, `uninstall_rule`,
`deferral_hours` — and nothing else. The server *has* one (`packages.name`) and
does not send it.

The only server-supplied identifier that reaches the endpoint is
`detection_rule.name` on a `package_installed` rule, so that is what the agent
reads. Its absence is a refusal that says so. **The agent does not invent an
identifier and does not parse one out of `install_args`.**

**The server-side fix:** add an explicit `package_id: str | None` to
`AppDeployParams` (additive, so it does not break deployed agents) and set it
from `packages.name` for manager-sourced types.

---

## 5. The idle budget, measured

`CLAUDE.md`: *"Heavy capabilities stay dormant and only allocate/initialize
when a job for them actually runs, then release."* These two are as heavy as
capabilities get — a process spawn and a multi-gigabyte download — so the
dormancy claim is the one that matters.

Every number below is produced by `testing.Benchmark` called from an **ordinary
`Test`** through `internal/budget`, and **compared against a threshold**. `go
test -bench` exits 0 on a regression; a budget stated as a benchmark is a log
line, not a gate.

| falsifier | measurement (this runner, `go test` without `-race`) |
|---|---|
| 1a. anything allocated on the idle path | **0 allocs/op, 0 B/op** over 533,898,888 iterations, goroutine delta **0** |
| 1b. a file descriptor held between jobs | **6 open before, 6 after** 100 script runs + 100 deployments; 0 scratch directories survived |
| 2a. RSS after one job | **−1,146,880 bytes** (it shrank), 73 allocs, 15,080 bytes allocated |
| 2b. RSS after 2,000 script runs | **+339,968 bytes** (limit 4 MiB), 49 allocs/op, 2,441 B/op |
| 2c. retained heap, 20 vs 2,000 runs | **0 and 0, delta 0** (allowance 64 KiB) |
| 2d. RSS after 1,000 deployments | **−45,056 bytes**; retained delta **144 bytes** |
| 3. cost of one script run | **49 allocs/op, 2,365 B/op** (ceiling 64 KiB) |
| 4. a flood of installer output | 1 KiB in → 2,176 B/op; **100 MiB in → 34,816 B/op** (wire cap 16 KiB); `capBuffer` retained 1,048,576 of 100 MiB |

**Wall-clock CPU is not asserted**, and that is a deliberate refusal for the
reason `internal/budget`'s package comment gives: a shared runner's variance
exceeds the signal, a threshold loose enough not to flake is loose enough to
pass a real regression, and a flaky budget test gets skipped — which reads as
coverage while measuring nothing. `~0% CPU at idle` stays a **design target**
for this capability.

**Falsifier 4 found a real bug** rather than confirming a claim. `wireLog`
sanitised the whole stream and truncated afterwards, so a 100 MiB installer log
allocated **104,876,080 bytes** against a 16 KiB wire cap. `spawn` already caps
capture at 1 MiB so production never saw it, but a bound that depends on every
caller being careful is not a bound. Truncating before sanitising brings it to
34,816 B/op.

### The process bound, also measured

`exec.CommandContext` does **not** bound a shell-out: cancelling the context
kills the child, but `Wait` blocks until the stdout pipe closes, and a
grandchild that inherited the pipe holds it open. That is the exact shape of an
MSI bootstrapper (spawns `msiexec` children and exits) and of `choco` (spawns
PowerShell which spawns everything else).

Measured here with a helper whose grandchild inherits stdout and sleeps 30 s,
under a **200 ms** deadline:

- with `cmd.WaitDelay = 5s`: **5.203 s**
- with `cmd.WaitDelay` removed: **30.041 s**

---

## 6. Not implemented, and deliberately

Each of these is a decision, not an oversight. Where the reason is "a second
way to put a program on an endpoint", the bar for reversing it is a design
discussion rather than a patch.

| thing | status | why |
|---|---|---|
| `command_exit_zero` detection rule | **not implemented**, reports `detected_after: null` | it is a second arbitrary-execution path, and the command would arrive inside `detection_rule`, an `additionalProperties: true` free-form dict — unlike a script body, which is a first-class field with an authoring flow, an RBAC gate (`deploy.script.manage`) and a per-version audit trail. Both are covered by the same signature; this is about how many places in this system can put a program on an endpoint. |
| `registry_value`, `package_installed` detection | **not implemented**, `detected_after: null` | needs a Windows registry read and an installed-software enumeration. `inventory` already does the second; wiring it in is a later cycle. `null` is the honest answer — collapsing "cannot evaluate" into `false` would show every successful install of a registry-detected package as "installed but not detected", i.e. an operator chasing a non-existent problem on every device in the fleet. |
| `dmg` installer type | **refused** | mounting a DMG, copying an `.app`, and detaching is a multi-step flow with its own failure modes. |
| `deb`, `rpm` | **refused** | the fleet is Windows 11 and macOS. |
| `script` installer type | **refused** | it carries no interpreter, and `script.run` is the signed path for running a script. |
| `exe` uninstall | **refused** | `AppDeployParams` has no field for an uninstall command. Probing the Windows uninstall registry keys and executing whatever string is there would be the agent choosing a program to run as SYSTEM from data the signature does not cover. |
| `.pkg` uninstall | **refused** | a macOS `.pkg` carries no uninstaller; the only general approach is deleting the files in its receipt, which is a destructive filesystem operation driven by a manifest, not an install engine. |
| `deferral_hours` | **carried, ignored** | there is no user-notification channel: no agent UI, no toast, and no session to show one in when running as SYSTEM. Silently ignoring it is honest; pretending to defer is not. |
| `log_ref` | **always `null`** | there is no agent-facing artifact-upload route, so the truncated `log` is all the operator gets. |
| `install_args` with quoted arguments | **refused** | see §7. |
| site-server artifact cache (spec §6) | **not implemented** | a documented seam in the spec; the `Fetcher` interface is where it would go. |

---

## 7. The `install_args` limitation, stated plainly

`install_args` is split on whitespace into argv **elements**, and every token
must match a conservative allowlist:

```
A-Z a-z 0-9 . _ - / \ : = + , @ # { }
```

An operator therefore **cannot** pass a quoted path containing a space:
`TRANSFORMS="C:\Program Files\x.mst"` is refused.

That is a real cost and it is taken deliberately. On POSIX the allowlist is
belt-and-braces — there is no shell to interpret a metacharacter. On Windows it
is not: `msiexec`, `winget` and `choco` each re-parse their own arguments, and
cmd-style escapes (`^`, `!`, `%`) survive several of those layers. Writing a
quoting parser would mean shipping a claim about `CommandLineToArgvW`,
`msiexec`'s property parser and `choco`'s PowerShell layer that this
environment cannot test against any of them. A repaired argument is an argument
nobody reviewed.

If a package genuinely needs a spaced path, the two ways forward are a
structured `install_args: list[str]` on the wire (additive) or a response file
staged next to the installer.

---

## 8. What has never executed

The honest list. The CI container is Linux with no MSI, no `.pkg`, no `winget`,
no `choco` and no PowerShell, so the following is **compiled and vetted for
`GOOS=windows` and `GOOS=darwin` and has never been run**:

1. **`perm_windows.go` — the scratch directory's DACL.** It applies
   `D:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)` with
   `PROTECTED_DACL_SECURITY_INFORMATION`. The POSIX half (`0700`, re-asserted
   and read back) *is* tested. Falsifying observation on a real endpoint:
   `icacls <scratch dir>` showing any principal other than SYSTEM and
   Administrators, or showing inherited ACEs.
2. **Every real installer invocation.** The *argv* for `msiexec`, `winget`,
   `choco` and `installer(8)` is asserted string-for-string on Linux, because
   the planners are pure functions of `(goos, params, paths)`. What those argv
   *do* on a real Windows 11 or macOS box is unverified. In particular: whether
   `winget uninstall` accepts `--exact` alongside `--id`, and whether
   `installer -pkg` tolerates extra `install_args`, are reasoned about and not
   observed.
3. **PowerShell, at all.** `-File` semantics, the `.ps1` extension
   requirement, and how `-ExecutionPolicy Bypass` interacts with an
   AppLocker/WDAC policy are untested.
4. **The exit-code map on a real installer.** `3010` and `1641` are treated as
   successes and `1605` is not; that is from the Windows Installer error
   documentation, not from a run.
5. **Defender / EDR reaction.** No claim is made that any of this passes a
   real ASR rule set. §3 exists so that can be evaluated before deployment
   rather than discovered.
6. **The `https://` artifact fetch.** `defaultFetcher`'s refusal branches are
   tested; the actual HTTP GET, its redirect behaviour and its interaction with
   a presigned URL are not, because there is no route to point it at (§4.1).

What **is** exercised end-to-end through the real `spawn`: a `bash` script body
written to a restricted file and executed, its stdout and stderr captured, its
exit code mapped, its `timeout_s` bounding a real `sleep 30`, a process that
floods 64 MiB to stdout, a process whose grandchild holds the pipe past the
deadline, a process that reads stdin, and a program that does not exist.
