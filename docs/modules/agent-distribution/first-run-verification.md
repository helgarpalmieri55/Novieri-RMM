# First-run verification: Windows and macOS

**Why this document exists.** `spec.md` §3 records that
`agent/packaging/windows/` and `agent/packaging/darwin/` have **never been
executed by anyone**, and the console marks those release rows *never
executed* for that reason. That marker is retired by **evidence**, not by
somebody feeling confident, and this is the evidence: the exact sequence to run
once on one real machine of each kind, what to observe at every step, and — for
each observation — **what its absence would falsify**.

**Read this before a fleet rollout, not after one.** Managed distribution
(`spec-managed-distribution.md`) makes the first Windows install potentially
three hundred installs at once. A defect found on machine one is a defect; the
same defect found on machine three hundred is an incident, and on the GPO path
it is a crashing SYSTEM service in every event log in the domain.

**What this is not.** It is not a substitute for the structural checks in
`agent/packaging/packaging_test.go`, and they are not a substitute for it. CI is
`ubuntu-latest`: it can prove that `install.ps1` balances its braces, that the
token reaches no service definition, and that the unenrolled-start guard is
present. It cannot prove that the SCM accepts the dispatcher, that `Set-Acl`
behaves, that a stop is clean, or that TCC lets a launchd daemon read system
inventory. Those need a machine.

**Scope.** Two platforms, three paths:

| Part | Platform | Path |
|---|---|---|
| A | Windows | the one-machine install command (`spec.md` §4.4) |
| B | Windows | managed distribution: install and enrol as two steps (§5 of the managed spec) |
| C | macOS | the launchd daemon |

Parts A and B can share one machine if you run Part A's uninstall (A9) first.

---

## 0. Before you start

### 0.1 What you need

- A control plane you can reach from the test machine on **443 and 8443**, and
  an account holding `enrollment.manage` (plus `agent.release.manage` if you
  are publishing the artefact yourself). `deploy/README.md`, "Publishing the
  agent to the console".
- **One Windows machine you are willing to break.** A VM with a snapshot is
  ideal, because several steps below are more informative if you can put the
  machine back. Windows Server 2019+ or Windows 10/11. Local administrator.
- **One Mac you are willing to break.** Apple Silicon is the more important of
  the two architectures, because the ad-hoc signature requirement (`agent/packaging/README.md`,
  "Code signing") is arm64-only — an Intel Mac cannot falsify it.
- The artefact and its **SHA-256 from the console**, and a **single-use
  enrollment token** (Part A) or a **deployment token** (Part B).

### 0.2 Turn on the two audits that make the token claims checkable

**On Windows, before installing anything.** Without these, the "the token never
enters argv" claim is untestable on this machine and step B6 proves nothing —
it would pass by there being no data rather than by there being no token.

```powershell
# Elevated. Records every process creation, WITH its command line.
auditpol /set /subcategory:"Process Creation" /success:enable
reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System\Audit" `
  /v ProcessCreationIncludeCmdLine_Enabled /t REG_DWORD /d 1 /f
```

Note the timestamp you did this at. Everything after it is on the record.

### 0.3 Record as you go

Fill in §5's table as you execute. A step you skipped and a step that passed
look identical afterwards, which is the whole reason `spec.md` §3 is worded the
way it is.

---

## A. Windows — the one-machine path

### A1. Run the one console command, elevated

From the console: **Platform → Agent Packages → Install command**. It gives you
a **single, self-contained command** — a presigned URL, the digest, a single-use
token, **and the reviewed `install.ps1` carried inside it, base64-encoded**. Run
it in an **elevated** PowerShell. It downloads and verifies the binary, decodes
`install.ps1` to a temp file, puts the token in `ITS_AGENT_ENROLL_TOKEN`, and
runs `install.ps1 -Binary <exe> -Url https://<your-console>` in a child
PowerShell with `-ExecutionPolicy Bypass`.

**Observe.** The command's own SHA-256 comparison passes (computed with
`[Security.Cryptography.SHA256]`, **not** `Get-FileHash` — the cmdlet the field
hit as `not recognized`), then the script prints `enrollment token written to
C:\ProgramData\ITSuite\agent\enroll-token (SYSTEM and Administrators only)` and
`its-agent installed.` in the same run. Installation and verification are no
longer two hand-run steps.

> **Falsifies:** a digest mismatch here means the catalogue and your build
> disagree, or the presigned GET is serving something else. Stop; do not
> continue. `deploy/README.md` step 3 is the check that should have caught it.
> `install.ps1 must run from an elevated PowerShell session` — you are not
> elevated. A `checksum mismatch` throw before the install runs — the download
> did not match the console's digest. Anything else is a defect: capture the
> full error including the line number.

If you would rather do the download and the digest by hand first:

```powershell
Invoke-WebRequest -Uri '<presigned-url>' -OutFile .\its-agent-windows-amd64.exe
[BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash(
  [IO.File]::ReadAllBytes('.\its-agent-windows-amd64.exe'))).Replace('-','')
# compare, case-insensitively, against the sha256 the console shows
```

### A2. The token never reached a command line

The console command puts the token in `ITS_AGENT_ENROLL_TOKEN` for the child
`install.ps1` to read and clears it afterward; it is never an argument. If you
prefer to keep it out of PSReadLine's history as well — the residual §4.4 owns —
run the pieces by hand with `Read-Host`, which does not echo:

```powershell
$sec  = Read-Host -AsSecureString 'enrollment token'
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
try   { $env:ITS_AGENT_ENROLL_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }

.\install.ps1 -Url https://<your-console> -Binary .\its-agent-windows-amd64.exe

$env:ITS_AGENT_ENROLL_TOKEN = $null
```

**Observe.** Either way, the script prints `enrollment token written to
C:\ProgramData\ITSuite\agent\enroll-token (SYSTEM and Administrators only)` and
then `its-agent installed.` Step B6 is where you confirm — from the 4688 record
— that no token value reached any argv, including the child `install.ps1`'s.

> **Falsifies:** `install.ps1 must run from an elevated PowerShell session` —
> you are not elevated. `No its-agent.exe found` — pass `-Binary`. Anything
> else is a defect: capture the full error including the line number.

### A3. The service exists, with no arguments and no token

```powershell
Get-Service its-agent | Format-List Name, Status, StartType
sc.exe qc its-agent
(Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Services\its-agent').Environment
sc.exe qfailure its-agent
```

**Observe, all four:**

1. `Status : Running`, `StartType : Automatic`.
2. `BINARY_PATH_NAME : "C:\Program Files\ITSuite\its-agent.exe"` — **the path
   and nothing after it**. Every argument on a service command line is readable
   by any local user for the life of the process.
3. The `Environment` value holds **exactly two** entries,
   `ITS_AGENT_DATA_DIR=...` and `ITS_AGENT_ENROLL_URL=https://...`, and
   **nothing resembling the token**. Anyone who can read the service key can
   read this value, and it outlives the token by months.
4. `RESET_PERIOD : 300`, then three `RESTART` actions at 5000 / 15000 / 60000 ms.

> **Falsifies:** a token in (3) is the defect this whole packaging design
> exists to prevent — stop and report it. Arguments in (2) are the same defect
> wearing a different hat. No failure actions in (4) means `sc.exe failure`
> exited non-zero and the service has no recovery at all; `install.ps1` now
> throws on that rather than continuing, so seeing it means the throw is not
> working.

### A4. The data directory is SYSTEM + Administrators, and nothing else

```powershell
$acl = Get-Acl "$env:ProgramData\ITSuite\agent"
$acl.AreAccessRulesProtected
$acl.Access | Format-Table IdentityReference, FileSystemRights, IsInherited
```

**Observe.** `AreAccessRulesProtected` is `True` (inheritance is broken), and
the only two entries are `NT AUTHORITY\SYSTEM` and `BUILTIN\Administrators`,
both `FullControl`, both `IsInherited: False`.

> **Falsifies:** an inherited `BUILTIN\Users` entry means `SetAccessRuleProtection`
> did not take, and the device private key that lands in this directory is
> readable by every local user. This is the single most important observation
> in Part A — it is the NTFS equivalent of the `0700` the agent enforces itself
> on unix, and unlike that one it is enforced only by this script.

### A5. It enrolled, and the token is gone

```powershell
Test-Path "$env:ProgramData\ITSuite\agent\identity.json"     # want True
Test-Path "$env:ProgramData\ITSuite\agent\enroll-token"      # want False
```

**Observe.** `True` then `False`, within a few seconds of the service starting.
The agent reads the token once and **deletes it** the moment it has exchanged
it for a certificate, so the disappearance of that file *is* the success signal.

Then in the console: the device appears in the fleet list, `agent_status:
online`.

> **Falsifies:** `identity.json` absent and `enroll-token` still present means
> enrolment is failing. Read the service's own output (A6). A `403` at this
> point most often means the token was already spent, was revoked, or arrived
> with a UTF-8 BOM attached — the last is why `install.ps1` writes the file with
> `UTF8Encoding($false)` and why it must keep doing so.
>
> **Both absent** means the agent could not write the directory at all.

### A6. What the SCM thought, and how to see the agent's own output

The agent logs to **stderr** and nothing in this packaging registers an event
source, so the Application log has nothing from it. What *is* there is the
Service Control Manager's own view:

```powershell
Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='Service Control Manager'} -MaxEvents 20 |
  Where-Object { $_.Message -match 'its-agent' } | Format-List TimeCreated, Id, Message
```

**Observe.** A `7036` ("entered the running state") and nothing else.

> **Falsifies — and this is the one to look for:** event **7000** with *"The
> service did not respond to the start request in a timely fashion"*, or error
> **1053**, means the SCM dispatcher did not register. That is the
> `agent/internal/service/service_windows.go` half of this, which CI compiles
> for windows/amd64 and has never executed. A `7031`/`7034` loop means the
> agent is exiting and the failure actions are restarting it.

To watch the agent directly, stop the service and run the binary in the
console — `svc.IsWindowsService()` keeps the same binary usable that way:

```powershell
Stop-Service its-agent
$env:ITS_AGENT_DATA_DIR = "$env:ProgramData\ITSuite\agent"
& "$env:ProgramFiles\ITSuite\its-agent.exe"
```

Exit codes are load-bearing: **2** configuration (no identity and no token),
**3** enrolment refused, and 4/5/6 for the later stages.

### A7. A signed job round-trips

Restart the service, then from the console dispatch a `hello` job at this
device (`deploy/README.md` step 11d).

**Observe.** `"status": "succeeded"` with a `result`, and the audit chain
carrying `jobs.dispatched` and `jobs.succeeded`.

> **Falsifies:** this is the whole platform on this endpoint — the server signed
> the envelope with the key that never leaves OpenBao, and the agent verified
> that signature before executing anything. A job that reaches the device and
> fails verification is a Windows-specific defect in the signed-job path, which
> nothing on Linux would have shown.

### A8. Stop, restart, reboot

```powershell
Restart-Service its-agent
Get-Service its-agent
Restart-Computer            # then, after it comes back:
Get-Service its-agent
```

**Observe.** Running after each, and the device goes offline and back online in
the console rather than duplicating. A clean stop means the handler cancelled
the agent's context and it flushed its outbound buffer.

> **Falsifies:** a **second device row** after the reboot means the agent
> re-enrolled, which means it could not find its identity — check that
> `ITS_AGENT_DATA_DIR` in the service `Environment` is the directory
> `identity.json` is actually in.

### A9. Uninstall, and check what it left

```powershell
.\uninstall.ps1
Get-Service its-agent -ErrorAction SilentlyContinue      # want nothing
Test-Path "$env:ProgramFiles\ITSuite\its-agent.exe"      # want False
Test-Path "$env:ProgramData\ITSuite\agent"               # want False
```

**Observe.** The banner reads `its-agent removed from this endpoint.` — not
`its-agent PARTIALLY removed`. The device row, its certificate at the gateway
and the audit log all survive, on purpose; retire the device in the console.

> **Falsifies:** `PARTIALLY removed ... still present: ...its-agent.exe` means
> something still holds the image open after `sc.exe delete`. The credentials
> were removed by name regardless, which is why they are removed first.

---

## B. Windows — managed distribution, install and enrol as two steps

This is the path a GPO startup script or an Intune Win32 app runs, and it is
the one that reaches the whole domain. Start from a machine with no agent (A9,
or a fresh snapshot).

### B1. Stage the package and verify it once

Download the artefact **once**, on your own workstation, from **Platform →
Agent Packages → Managed deployment…**, and run the `Get-FileHash` comparison
the dialog gives you **before** the file reaches the share.

**Observe.** The digests match. Then copy the binary, `install.ps1` and
`its-agent-enroll.ps1` to the distribution point.

> **This is the last integrity check anybody performs on this path.** The
> endpoint does not verify anything: GPO and Intune copy a file and run it.
> After this point the only control is the ACL on that share, and because the
> binary is unsigned it is the *only* control — see
> [`authenticode-signing.md`](authenticode-signing.md).

Check the share's ACL now, while you are looking at it:

```powershell
(Get-Acl \\<dfs-or-sysvol>\its-agent).Access |
  Where-Object { $_.FileSystemRights -match 'Write|Modify|FullControl' } |
  Format-Table IdentityReference, FileSystemRights
```

**Observe.** Only administrators you trust. `Authenticated Users` or `Domain
Users` with write access here is remote code execution as SYSTEM on every
machine in scope.

### B2. Install without enrolling

```powershell
.\install.ps1 -Binary .\its-agent-windows-amd64.exe -NoStart
```

**Observe.** `service registered, not started (-NoStart)`.

```powershell
Get-Service its-agent | Format-List Name, Status, StartType
```

`Status : Stopped`, `StartType : Automatic`.

Now the important negative — **run `install.ps1` with no token and no
`-NoStart`** on a second fresh machine, or after `Remove-Item` of the data
directory:

**Observe.** `service registered and NOT started: this endpoint has no
enrollment token and no identity...`

> **Falsifies — this is falsifier 7 of the managed spec, on real hardware.** If
> the service starts here, it exits 2, the `sc.exe failure` actions restart it,
> and you have just reproduced the restart loop that a GPO rollout would put on
> every machine in the domain. Watch for it with the A6 `Get-WinEvent` query:
> repeated 7031/7034 for `its-agent` is the signature.

### B3. Enrol, with the token from stdin

The channel's own secret mechanism supplies the token. Test the stdin form,
because it is the one a pipeline uses and the one no PowerShell pipeline can
exercise:

```powershell
cmd /c "type \\<secret-share>\token.txt | powershell -NoProfile -ExecutionPolicy Bypass -File .\its-agent-enroll.ps1 -TokenFromStdin -Url https://<your-console>"
```

**Observe.** `enrollment token written to ...` then `started its-agent`, and
A5's two `Test-Path` results.

> **Falsifies:** if this **hangs**, stdin was not actually redirected — that is
> the trap the `-TokenFromStdin` documentation warns about, because a
> PowerShell pipeline (`'tok' | .\its-agent-enroll.ps1 -TokenFromStdin`) leaves
> the process's stdin on the console and the script waits for somebody to type.
> Confirm it by running the `.\its-agent-enroll.ps1` form with
> `$env:ITS_AGENT_ENROLL_TOKEN` set instead; if that works and the redirect
> does not, report the redirect form.

Also test the environment form and the two refusals:

```powershell
.\its-agent-enroll.ps1 -Token abc123 -Url https://<your-console>
```

**Observe.** It throws, and the message names Security event 4688 and the two
supported forms. **The parameter is declared precisely so that it can be
refused with a reason** rather than answered with PowerShell's own
"A parameter cannot be found that matches parameter name 'Token'".

```powershell
.\its-agent-enroll.ps1 -Url https://<your-console>      # on an already-enrolled machine
```

**Observe.** `this endpoint is already enrolled (...); nothing to do`, exit 0.

> **Falsifies:** anything else here is a duplicate device record per boot, since
> a GPO startup script runs at every boot. This is the single most consequential
> no-op in the packaging.

And the one that was a real defect until 2026-08-26 — run the enrol helper on a
machine where **install.ps1 has never run**:

```powershell
.\its-agent-enroll.ps1 -Url https://<your-console>       # service not installed
Test-Path "$env:ProgramData\ITSuite\agent\enroll-token"
```

**Observe.** It throws `the service its-agent is not installed; run install.ps1
first`, **and the second line answers `False`.**

> **Falsifies:** `True` means the script wrote the deployment credential to disk
> before discovering it had nothing to spend it on. On a fleet that is your
> multi-use token sitting on every machine the install missed, with nothing to
> remove it.

### B4. Two installs in a row (the upgrade case)

Re-run `install.ps1` over a **running** agent, pointing at a newer binary.

**Observe.** It completes, and `its-agent.exe` has the new timestamp.

> **Falsifies:** *"The process cannot access the file because it is being used
> by another process"* means the service is not being stopped before the binary
> is replaced. That was the behaviour before 2026-08-26 and it failed only on
> upgrade, never on a first install.

Then re-run `install.ps1` **without** `-Url`:

```powershell
(Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Services\its-agent').Environment
```

**Observe.** `ITS_AGENT_ENROLL_URL` is **still there**, carried over from the
previous install.

> **Falsifies:** its disappearance means a re-install erases the URL, which
> strands a staged-but-not-yet-enrolled machine with a token it can never spend.

### B5. Reboot, twice

**Observe.** The agent comes up at boot without intervention, once, both times,
and no second device row appears.

### B6. The token never entered argv — checked, not asserted

With §0.2's auditing on:

```powershell
Get-WinEvent -FilterHashtable @{LogName='Security'; Id=4688} -MaxEvents 400 |
  Where-Object { $_.Message -match 'its-agent|install\.ps1|powershell' } |
  ForEach-Object { ($_.Message -split "`n" | Select-String 'Process Command Line').ToString() }
```

**Observe.** Every recorded command line for `install.ps1`,
`its-agent-enroll.ps1` and `its-agent.exe` carries **the script path, `-Url`,
`-Binary`, `-NoStart`, `-TokenFromStdin` — and no token value.** Search the
output for the token's first eight characters and get nothing.

> **This is the empirical form of the claim `packaging_test.go` can only make
> structurally.** If a token value appears here, some path is putting it in
> argv, and 4688 records are collected by every EDR in the estate and shipped
> to a SIEM that keeps them for a year.

Check the shell history too:

```powershell
Select-String -Path (Get-PSReadLineOption).HistorySavePath -Pattern '<first 8 chars of the token>'
```

**Observe.** No match, **if** you used A2's `Read-Host -AsSecureString` form.

> **Expected to MATCH, and honestly so, if you typed
> `$env:ITS_AGENT_ENROLL_TOKEN = '<token>'` at the prompt.** PSReadLine records
> the whole line. The scripts' refusal is about *argv*; the environment-variable
> form is your history's problem, which is why the interactive recipe uses
> `Read-Host`. Record which form you used.

---

## C. macOS

### C1. Verify and place the binary

```sh
shasum -a 256 its-agent-darwin-arm64      # compare against the console's sha256
sudo mkdir -p /opt/its-agent && sudo cp -R agent/packaging/darwin /opt/its-agent/
```

### C2. Confirm the ad-hoc signature the kernel demands — before installing

On Apple Silicon the **kernel** kills an arm64 Mach-O with no code signature at
`exec`. Not Gatekeeper, not quarantine, not notarization: it simply does not
start. `agent/packaging/machosign_test.go` verifies this by arithmetic in CI;
this is the same claim from the other side.

```sh
codesign -dv --verbose=4 ./its-agent-darwin-arm64 2>&1 | head -20
```

**Observe.** `flags=0x20002(adhoc,linker-signed)`, `CodeDirectory ... hashes=1704+...`,
`Signature=adhoc`.

```sh
spctl -a -vv ./its-agent-darwin-arm64
```

**Observe — and this is expected to FAIL:** `rejected (the code is valid but
does not seem to be an app)` or `code has no resources but signature indicates
they must be present`. **That is not a defect.** `spctl` is Gatekeeper, which
assesses *quarantined* artifacts; an MDM-delivered payload and a file you
copied over SSH are not quarantined. It is #2 in the README's signing table and
it is open for commercialization, not for this rollout. Record the exact
message.

### C3. Install, with the token from the environment

```sh
sudo ITS_AGENT_ENROLL_TOKEN='<token>' /opt/its-agent/darwin/install.sh \
  --url https://<your-console> --binary ./its-agent-darwin-arm64
```

**Observe.** `enrollment token written to /var/lib/its-agent/enroll-token (0600,
root)`, then `its-agent installed.`

Also test the refusal and the stdin form:

```sh
sudo /opt/its-agent/darwin/install.sh --token abc123 --url https://x   # must refuse, with the reason
printf %s '<token>' | sudo /opt/its-agent/darwin/install.sh --token-stdin --url https://<your-console>
```

> Unlike PowerShell, a shell pipe **does** feed this script's stdin, so the
> `--token-stdin` form works exactly as written here.

### C4. Permissions on disk

```sh
ls -ld /var /var/lib /var/lib/its-agent /var/log/its-agent
ls -l  /var/lib/its-agent
ls -l  /Library/LaunchDaemons/com.toeshee.itsagent.plist
```

**Observe.**

- `/var/lib/its-agent` is `drwx------  root  wheel` (0700).
- `enroll-token` is `-rw-------  root  wheel` (0600).
- the plist is `-rw-r--r--  root  wheel` (0644) — launchd refuses a plist that
  is group- or world-writable.
- **`/var/lib` itself.** It does not exist on a stock macOS, so the installer
  creates it. Record its mode. If BSD `install -d -m 0700` applied `0700` to
  the parent as well as the leaf, `/var/lib` is now root-only, which is not
  what anyone intends and is exactly the class of thing an unexecuted installer
  hides. **This is an open question, not a known defect** — GNU `install` does
  not behave that way and nobody has run BSD `install` here.

> **Falsifies:** anything group- or world-readable under `/var/lib/its-agent`
> exposes the device private key.

### C5. The daemon is loaded, running, and carries no token

```sh
sudo launchctl print system/com.toeshee.itsagent
```

**Observe, in that output:**

- `state = running` and a `pid`.
- `program = /usr/local/libexec/its-agent` with **`arguments` holding the
  binary and nothing else** — argv is visible in `ps` to every user.
- `environment` showing `ITS_AGENT_DATA_DIR` and `ITS_AGENT_ENROLL_URL`, and
  **no token**. This output is printable by any user; the plist under
  `/Library/LaunchDaemons` is world-readable.
- **no `sockets` entry.** Invariant #2: the agent opens no listening port and
  launchd binds nothing on its behalf.

```sh
ps -Ao pid,args | grep '[i]ts-agent'
```

**Observe.** The binary path alone.

### C6. It enrolled

```sh
test -f /var/lib/its-agent/identity.json && echo enrolled
test -f /var/lib/its-agent/enroll-token  && echo "STILL HOLDING A TOKEN"
tail -50 /var/log/its-agent/agent.log
```

**Observe.** `enrolled`, no second line, and the log showing `enrolling` then
`enrolled` then `gateway connected`. The device appears in the console.

### C7. TCC — expect the first real finding here

The README's own prediction is that *"the first real finding on a Mac will be
TCC, not signing"*. The agent collects system inventory as a root daemon, and
macOS gates several of those reads behind TCC.

```sh
log stream --predicate 'subsystem == "com.apple.TCC"' --info &
# then let an inventory job run against this device from the console
```

**Observe.** Whether any inventory field comes back empty on macOS that is
populated on Linux, and whether TCC logs a denial naming
`/usr/local/libexec/its-agent`.

> **Falsifies:** a TCC denial means a PPPC profile is needed, delivered by MDM.
> Note that against an **ad-hoc-signed** binary a PPPC profile can only pin by
> **path**, so anything that replaces the file at that path inherits the grant —
> that is the concrete reason to want a Developer ID signature, and it belongs
> in the record either way.

### C8. A signed job round-trips

Dispatch `hello` at this device. Same expectation as A7.

### C9. Restart, and reboot

```sh
sudo launchctl kickstart -k system/com.toeshee.itsagent
sudo launchctl print system/com.toeshee.itsagent | head -20
sudo reboot
```

**Observe.** Running again after the kickstart (a clean SIGTERM, 30 s
`ExitTimeOut`, then restart) and running again after the reboot (`RunAtLoad`),
with **no second device row**.

### C10. The `launchctl disable` trap

```sh
sudo launchctl bootout system/com.toeshee.itsagent
sudo launchctl disable system/com.toeshee.itsagent
sudo ITS_AGENT_ENROLL_TOKEN='' /opt/its-agent/darwin/install.sh --url https://<your-console>
```

**Observe.** The install still succeeds and the daemon runs, because
`install.sh` runs `launchctl enable` **before** `bootstrap`.

> **Falsifies:** `Bootstrap failed: 5: Input/output error` or `Service is
> disabled` means the enable/bootstrap order has regressed. launchd's disabled
> database survives reboots and reinstalls, so an install that cannot clear it
> can never recover from one earlier `launchctl disable`.

### C11. Uninstall

```sh
sudo /opt/its-agent/darwin/uninstall.sh
sudo launchctl print system/com.toeshee.itsagent   # want "Could not find service"
ls /var/lib/its-agent /usr/local/libexec/its-agent /Library/LaunchDaemons/com.toeshee.itsagent.plist
```

**Observe.** All three gone. `--keep-state` leaves `/var/lib/its-agent` and only
that.

---

## D. Retiring the "never executed" marker

The marker in the console and the row in `spec.md` §3 come off when — and only
when — **Part A, Part B and Part C have each been executed end to end on a real
machine**, with §5's table filled in and any defect either fixed or recorded.

Two things this does **not** license:

- **One Windows machine is not a fleet.** It retires "never executed"; it does
  not retire "never deployed at scale". The next honest wording for a Windows
  row is *executed once, not rolled out*.
- **An Intel Mac does not cover Apple Silicon**, and vice versa. C2 is arm64's
  question and TCC is both architectures'.

Update, in one change:

1. `agent/packaging/README.md`, "What is untested" — the two bullets that say
   the Windows service and the macOS daemon have never run.
2. `docs/modules/agent-distribution/spec.md` §3 — the two `Never executed by
   anyone.` cells, and the paragraph above them.
3. The console's per-row marker (`spec.md` §4.5's platform-honesty requirement,
   asserted by `AgentReleasesPage.test.tsx`), which is a test as well as a
   string — expect to change both.
4. `deploy/README.md`, "What publishing does not do" and step 11c's ⚠ marker.

**Do not update any of them from a partial run.** A marker removed on the
strength of "it installed fine" and nothing else is worse than the marker,
because the next reader has no way to tell which half was checked.

---

## 5. The record

| # | Check | Result | Notes |
|---|---|---|---|
| 0.2 | 4688 command-line auditing enabled, with a timestamp | | |
| A1 | digest verified on the endpoint | | |
| A2 | install completes elevated | | |
| A3.1 | service Running / Automatic | | |
| A3.2 | binPath carries no arguments | | |
| A3.3 | service `Environment` carries no token | | |
| A3.4 | failure actions configured | | |
| A4 | data dir ACL protected, SYSTEM + Administrators only | | |
| A5 | `identity.json` present, `enroll-token` gone | | |
| A6 | no 7000 / 1053 / 7031 loop | | |
| A7 | signed `hello` job round-trips | | |
| A8 | survives restart and reboot, one device row | | |
| A9 | uninstall reports fully removed | | |
| B1 | staging digest verified; share write-ACL reviewed | | |
| B2 | `-NoStart` registers without starting | | |
| B2 | no-token install does NOT start (no restart loop) | | |
| B3 | stdin enrol works via an OS-level redirect | | |
| B3 | `-Token` refused with the reason | | |
| B3 | re-run on an enrolled endpoint is a no-op | | |
| B3 | enrol with no service installed leaves NO token on disk | | |
| B4 | re-install over a running agent succeeds | | |
| B4 | re-install without `-Url` keeps the URL | | |
| B5 | two reboots, one device row | | |
| B6 | no token value in any 4688 command line | | |
| B6 | shell history checked; which token form was used | | |
| C2 | ad-hoc signature present on arm64 | | |
| C2 | `spctl` verdict recorded (expected: rejected) | | |
| C3 | `--token` refused; `--token-stdin` works | | |
| C4 | 0700 state dir, 0600 token, 0644 plist | | |
| C4 | mode of `/var/lib` after install | | |
| C5 | no `sockets`, no token in `launchctl print` | | |
| C6 | enrolled; token file gone | | |
| C7 | TCC denials, if any | | |
| C8 | signed `hello` job round-trips | | |
| C9 | survives kickstart and reboot, one device row | | |
| C10 | install recovers from `launchctl disable` | | |
| C11 | uninstall leaves nothing | | |

Anything in the "Result" column other than the expected observation is a defect
report, not a note. File it against `agent/packaging/` with the exact command
and the exact output.
