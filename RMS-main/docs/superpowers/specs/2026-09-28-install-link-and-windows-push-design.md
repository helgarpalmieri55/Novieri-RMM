# Install link and Windows push — design

**Status:** approved in brainstorming by the operator, 2026-09-28. Awaiting spec review.
**Module:** agent-distribution (`docs/modules/agent-distribution/`).
**Two deliverables, two PRs, in this order:** Part 1, the install link; Part 2, Windows push over WinRM.

---

## 0. What the operator asked for, and what it overturns

> "the tech guy opens the link on the machine and the server determines
> everything for that asset automatically, we only use 1 enroll link for all
> devices, we can add a review step on the server for a tech to review all
> assets that joined and approve them to be there but even without his approval
> the agent must be installed, also add the option to enable or disable this
> agent url installation so when we complete the full deploy we can turn it off"
>
> "…and the ability to push install from the platform using AD admin windows
> credentials"

Choices made in brainstorming, in order:

| # | Question | Operator's answer |
|---|---|---|
| Q1 | Link first, push first, or both | Both, in one design |
| Q2 | Who opens the link | IT staff at the machine |
| Q3 | How the installer proves it may join | One shared link for all devices; no sign-in; review afterwards; the agent installs whether or not it is approved; an on/off switch |
| Q4 | What an unapproved machine may do | Visible, but held back |
| Q5 | Where the link works | Only from networks the operator lists |
| Q6 | Windows transport | WinRM, from any site server |
| Q7 | Push account | An existing Domain Admin |

**Recorded decisions this design overturns, as the operator's call:**

1. `agent/packaging/README.md` — *a token must never reach a distributable
   artefact.* The link code rides in the downloaded file's **name** (not its
   bytes), and stays in the Downloads folder of every machine installed this
   way. Bounded by §1.6.
2. `docs/modules/agent-distribution/spec.md` §7 — *no unauthenticated download
   endpoint.* The link page is public by design. Bounded by §1.6.
3. `docs/modules/agent-distribution/spec-push-install.md` **D10** — *SSH first;
   Windows transport is a separate decision.* This is that decision. §2.3 is
   the replacement for §7.3's relay accounting.
4. The *Before a push can run* guidance "not a domain administrator" becomes a
   standing warning rather than advice the console acts on (Q7).

**Invariants in `CLAUDE.md` that do not change, and how each still holds:**

- **1 (signed jobs).** Every job, including the new `agent.uninstall` and the
  WinRM push, is created by `JobEngine.create`, signed, and verified by the
  agent. The link path issues a certificate; it dispatches no job.
- **2 (outbound mTLS, no listening port).** The link-installed agent connects
  out exactly like any other. The WinRM push leaves from a site server, never
  from the control plane.
- **2a (push to unenrolled machines).** Every clause still holds for WinRM:
  (a) the site server is the only originator, no fallback; (b) no command line
  travels — the WinRM steps are compiled into the agent; (c) the push delivers
  a single-use enrollment token and mints no identity; (d) vouched asset row +
  active authorization + scoped credential; (e) own permission codes, never
  automated; (f) named addresses only, batch ceiling. 2a's "what it does not
  claim" paragraph stays true and is sharpened, not removed, by §2.3.
- **3 (vault).** The Windows credential is a vault secret, decrypted only inside
  the push job's parameters (as the SSH credential is today).
- **4 (audit).** Link on/off, network changes, regeneration, every link
  enrollment, approve, reject, every push: audited.
- **5 (tenancy).** A link maps to exactly one org. Every new table carries
  `org_id` under RLS.
- **6 (RBAC).** New permission codes (§3); the link itself is the only
  authorization for its own enrollment, as an enrollment token is today.

---

## 1. Part 1 — the install link

### 1.1 Settings (Agent packages → *Install link*)

- **On/off switch.** Off by default. Optional *turn off automatically on* date;
  a worker beat switches it off at that instant and audits it.
- **Allowed networks.** A list of CIDRs (IPv4 and IPv6). An enrollment or page
  request whose source address is outside every entry is answered exactly like
  a link that does not exist (404). The source address is the client address
  Traefik appends to `X-Forwarded-For` (the **rightmost** entry, the one
  Traefik itself wrote — earlier entries are whatever the client sent). The
  API has no trusted-proxy handling today, so this adds it: the header is
  honoured only when the direct peer is inside a new
  `ITS_TRUSTED_PROXY_CIDRS` setting (default: the compose network Traefik
  sits on), and ignored from any other peer. **At least one network is
  required to switch the link on** — Q5 chose "only networks I list", so an
  empty list is not "anywhere".
- **The link:** `https://<console>/i/<code>`, where `<code>` is 32 characters
  from a URL-safe alphabet (≥ 160 bits). Stored only as a SHA-256 hash; shown
  in full only right after creation or regeneration (like an enrollment token).
- **Regenerate.** A new code; the old one stops working immediately.
- **Rate limit.** At most 50 link enrollments per org per rolling hour
  (configurable 1–500). Over the limit: 429, audited once per hour.

Table `install_links` (one row per org, RLS): `org_id`, `enabled`,
`code_hash`, `networks` (cidr[]), `disable_at`, `hourly_limit`, `created_by`,
timestamps.

### 1.2 The page at the link

Public, no sign-in. On `GET /i/<code>`: link on + code matches + source in an
allowed network, else 404 with no detail.

- **Windows:** a *Download* button. `GET /i/<code>/windows?arch=amd64|arm64`
  redirects to a 15-minute presigned URL for the **current stable** release
  for that platform, with `Content-Disposition` filename
  `ITMS-Agent-Setup_<code>.exe`. The bytes are the published artefact,
  untouched — the release signature (and later Authenticode) stays valid. The
  page picks the arch from the browser's client hints and offers the other.
- **macOS / Linux:** the page shows one command:
  `curl -fsSL https://<console>/i/<code>/install.sh | sudo sh`.
  `install.sh` is a fixed script that downloads the stable artefact for the
  machine's platform, verifies its SHA-256 against the value the server
  embeds, writes the code to the token file, and runs the existing
  `install.sh` logic.
- **No stable release on 1.6.0 or later for that platform:** the page says so
  and offers nothing. (The link installer needs an agent that understands it.)

### 1.3 The Windows self-installer (agent 1.6.0)

*(Amended 2026-09-28 as built: the file name carries the console (operator
decision D6a), and the review of Tasks 10–13 added steps 1, 2 and 4's
verification.)*

When `its-agent.exe` is started with **no arguments** (or exactly
`--elevated-install`, the UAC re-launch) and its own file name parses as
`ITMS-Agent-Setup_<host>[@<port>]_<code>.exe` (read from the right; a browser's
` (n)` / `(n)` suffix and any extension case tolerated; a DNS host, never an IP
literal; anything else is a normal start):

1. Pin DLL resolution to System32 (`SetDefaultDllDirectories`), first, in both
   phases.
2. Ask, Yes/No: *Install the ITMS agent and connect this computer to
   &lt;host&gt;?* The host comes from a file name and is unauthenticated; No
   installs nothing.
3. Re-launch itself elevated (UAC prompt). A tech approves it.
4. Hold its own file open read-only and unshareable for writing, deleting or
   renaming; stage a copy from that handle into `%ProgramFiles%\ITSuite`; check
   the staged bytes against `GET /i/<code>/windows.sig?arch=<a>&sha256=<hex>`
   (SHA-256, and the release signature under the compiled-in key); refuse on
   any mismatch. *(Final review I1: the digest selects the release, so a copy
   saved before a newer stable was promoted still installs; the console answers
   for any stable ≥ 1.6.0 Windows release of that arch with those bytes, or
   404. The request format is frozen from 1.6.0 on.)*
5. Set the data directory's owner (Administrators) and `install.ps1`'s ACL,
   create the token file fresh (`O_EXCL`), move the verified copy to
   `%ProgramFiles%\ITSuite\its-agent.exe` (plan D5), register and start the
   service with `https://<host>[:<port>]` as its enrollment URL.
6. Show a small window: *Installed — this computer will appear in ITMS within
   a minute.* Or the error, in plain words.

The code is never an **argument** -- not a flag, not in the UAC re-launch's
parameters, not in the service definition. It **is** in the installer's own
image path (`argv[0]`) while the installer runs, because that is the file's
name, so process-creation logging records it: Security event 4688 (with
command-line auditing), Sysmon event 1, and whatever an EDR or SIEM forwards
off the machine. The packaging README lists this beside the Downloads-folder
residual. The installer logic lives in `agent/internal/install/`; the service
links it but never calls into it, and linking it adds no package the service
does not already carry (`TestInstallLinksNothingTheServiceDoesNotAlreadyLink`).

### 1.4 Enrollment through the link

`POST /enroll/v1` gains an additive alternative: a request carrying
`link_code` instead of `token`. Older agents never send it; the existing token
path is unchanged.

Server, in one transaction: link on, code hash matches, source address
allowed, under the rate limit → create the device (`provenance = 'agent'`,
`review_state = 'pending'`), sign the agent's CSR through the ordinary
enrollment path, audit `agent.enrolled_via_link` with the source address.
Anything else: 404 (or 429 over the limit). The agent generated its keypair;
the private half never leaves it (unchanged).

### 1.5 Waiting for review

New column `devices.review_state`: `approved` (default — every existing
device and every token enrollment) or `pending`.

**The gate is in `JobEngine.create`**, the single place every job is born, so
no module can forget it. For a `pending` device, only these types may be
created:

| Allowed while pending | Why |
|---|---|
| `inventory.collect` | Q4: the machine is visible |
| `health.policy.apply`, `health.check.now` | Q4: health is reported. Monitoring only; remediation (approved actions) is a separate job type and is refused |
| `agent.cert.renew`, `agent.cert.install` | The certificate must stay alive until someone decides |
| `agent.uninstall` (new) | Reject |

Anything else: refused with `409 "waiting for review"`, before signing. Every
refusal names the device and the type. Because network sweeps and pushes are
jobs, a pending machine is never used as a sweeper or a push originator either.
**Promoting a pending machine to site server is refused** (409) — promotion is
an operator action, not a job, so it gets its own check.

**Devices page:** a *Waiting for review* filter, with a count badge in the nav.
Rows show how it joined (install link), from which address, and when.

- **Approve** → `review_state = approved`, audited.
- **Reject** → dispatch `agent.uninstall`, revoke the certificate, and remove
  the device row once the agent confirms or after 24 hours, whichever is first.
  Audited.

`agent.uninstall` (new, signed, every OS): stop and remove the service, delete
the binary, config and state directories, report success, exit. It carries no
parameters.

A notification event `device.joined_via_link` is available to notification
rules.

**A rejection that may have left the agent behind is not hidden.** *(Added
2026-09-29.)* The rejected row is a tombstone hidden everywhere (plan D1), so a
rejection that ended `timeout`, `agent_did_not_confirm` or
`agent_cannot_uninstall` (`devices.rejection_outcome`) would otherwise tell
nobody that the agent may still be on the computer. Such a row stays in
*Waiting for review*, and in its count, and nowhere else (it is still 404 by
id). It says in plain words that the agent must be uninstalled by hand, with an
**I removed it** button: `POST /devices/{id}/review/acknowledge-removal`
(`devices.review`, audited `device.removal_acknowledged`) sets
`devices.removal_acknowledged_at`, and the row is hidden again. Any other row is
a 409. The notification event `device.removal_incomplete` is published when one
of those three outcomes is stored, so a rule can email someone.

### 1.6 What bounds the link

A leaked code lets someone on one of the listed networks, while the link is on,
enroll a machine that then:

- appears in *Waiting for review*, where a tech sees an unknown name and rejects
  it;
- receives no job but inventory, health monitoring and certificate lifecycle —
  no software, script, patch, remote session, policy or credential.

Bounded further by the network list, the rate limit, the on/off switch, the
optional auto-off date and regeneration. The console shows, while the link is
on, how long it has been on and how many machines joined through it.

---

## 2. Part 2 — Windows push over WinRM

### 2.1 What changes in the existing push path

- `dispatch._device_os_arch` accepts `windows` (amd64, arm64).
- The transport enum already reserved `winrm` (`spec-push-install.md` §4). A
  Windows target is pushed over `winrm`; Linux and macOS stay on `ssh`.
- The site server's agent (1.6.0+) gains a WinRM client in the existing
  `pushinstall` capability. It is initialised only while a push job runs.

### 2.2 The steps, compiled into the agent

Same parameters as an SSH push (one address, transport, release version and
SHA-256, single-use enrollment token, credential) plus the target's DNS name.
On the target, over WinRM, the agent runs its own fixed PowerShell steps:

1. Create `C:\ProgramData\ITS Agent\push\` with SYSTEM + Administrators ACLs.
2. Download the artefact from the job's presigned URL (the target must reach
   the console — it will need to anyway, to connect); verify SHA-256; refuse on
   mismatch.
3. Write the enrollment token to the token file; install the service exactly as
   §1.3 step 2 does.
4. Delete the push directory.

No operator- or platform-authored command travels: the parameters carry values,
the script text is a constant in the agent binary.

### 2.3 Protecting the Domain Admin password

**Kerberos only.** The WinRM client offers Kerberos (SPNEGO) and nothing else:
NTLM, Basic and CredSSP are refused, including as a fallback when the target
asks for them. The push fails with *"this computer only offered NTLM; pushes use
Kerberos only"*.

Why that matters for Q7: with Kerberos, the password is used only against the
domain controller, to get a ticket for `HTTP/<target fqdn>`. What reaches the
target is a ticket only the real computer account can open. A machine that
answers at the address but is not that computer receives nothing it can decrypt
or replay, and a ticket bound to that SPN cannot be relayed to another service.
That closes the capture-and-relay exposure §7.3 of the push spec deferred
Windows for — for Kerberos. It is the reason NTLM is refused rather than
allowed as a convenience.

Requirements this puts on a push:

- **A DNS name.** Kerberos needs the target's name, not its IP. A target with
  no name on its asset row is refused before any credential is decrypted.
- **A domain controller reachable from the site server** (TCP/UDP 88). The
  probe (§2.4) checks it.
- **Encryption.** HTTPS (5986) when the target listens there; otherwise HTTP
  (5985) with Kerberos message encryption. Never unencrypted.

**What remains, stated plainly:**

- The site server holds the Domain Admin password in memory for the duration of
  a push. A compromised site server during a push is a compromised domain. A
  dedicated install account would bound that to workstations; the credential
  form says so, and a Domain Admin credential carries a standing warning on the
  Credentials page and in the push panel.
- A malicious machine that holds a genuine computer account in the domain *and*
  answers for another computer's name can receive a ticket for its own name
  only — not the target's. Kerberos does not make a compromised domain safe.

### 2.4 Probe and readiness

`agent.push_probe` for `winrm`, credential-free: resolves the name, checks 5986
then 5985 answer, and checks the site server can reach a KDC for the
credential's realm. Recorded on the asset like the SSH fingerprint.

*Before a push can run* stops saying Windows is refused, and gains:
*WinRM answers on the target* (from the probe) and *a domain controller is
reachable from the site server*.

### 2.5 Credential

New credential type *Windows domain account*: `DOMAIN\user` (or UPN) and a
password; vault-stored; scoped to address ranges exactly as push credentials
are today. The realm is derived from the domain and shown for confirmation.

### 2.6 What the operator sets up once

A Group Policy that enables WinRM on workstations (listener on 5985 or 5986,
firewall rule on the domain profile). The exact steps go in
`deploy/WINDOWS-PUSH.md`.

---

## 3. Permissions

| Code | Tier | Grants |
|---|---|---|
| `enrollment.link.manage` | admin | switch the link on/off, edit networks, regenerate, see the link |
| `devices.review` | tech | approve / reject machines waiting for review |

Windows push reuses `agent.push.run` and `agent.push.scope.manage`. None of these
is approvable by an automated or AI actor (2a(e) and the existing
`NON_APPROVABLE_REASONS`).

> **Amended by CLAUDE.md invariant 1c (2026-10-09):** an AI connected over the
> platform's MCP server acts as the person who connected it and may reach this
> act within that person's RBAC; the reservation above now governs automated
> and in-app-AI actors only.

---

## 4. Wire and version compatibility

- `POST /enroll/v1` with `link_code`: additive. Old agents never send it.
- `agent.uninstall`: a new job type; the server creates it only for agents
  reporting the capability (1.6.0+). For an older agent, Reject revokes the
  certificate and tells the tech the agent must be removed by hand.
- WinRM push: the site server must report the `winrm` push capability; an older
  site server is skipped with a reason, never used.
- The link page offers only a stable release ≥ 1.6.0.

---

## 5. Testing

**Part 1**

- Link off → 404; code wrong → 404; source outside networks → 404; each for the
  page, the download and the enrollment.
- Cannot switch on with an empty network list.
- Regenerate: the old code 404s immediately.
- Rate limit: the 51st enrollment in an hour → 429.
- `X-Forwarded-For` from a peer outside `ITS_TRUSTED_PROXY_CIDRS` is ignored;
  from Traefik, the rightmost entry is used, so a client-supplied header
  cannot claim an allowed network.
- `JobEngine.create` gate: every job type outside the allow-list refused for a
  pending device; each allowed type accepted; approving unblocks. A test
  enumerates the registered job types so a new type is refused by default.
- Approve / Reject: state, audit rows, `agent.uninstall` dispatched, certificate
  revoked.
- Tenancy: a link code of org A never enrolls into org B; RLS on
  `install_links`.
- Agent: file-name parsing (`ITMS-Agent-Setup_<host>[@<port>]_<code>.exe`,
  `… (1).exe`, upper-case extension, a bad host or port, no code → normal
  service start); no argument carries the code (the UAC re-launch is exactly
  `runas` + `--elevated-install`); the staged binary is refused unless its
  SHA-256 and release signature match `windows.sig`.
- Windows e2e on the GitHub Windows runner (as `agent-update-e2e` does): the
  self-installer installs the service, enrolls against a test control plane,
  and `agent.uninstall` removes it cleanly; the installed owner, ACL and
  recovery actions are read back; a bad host and a file that is not the offered
  release are refused with no service created. On the Linux runner,
  `agent.uninstall` removes a systemd install (`TestUninstallLinux`). macOS
  `agent.uninstall` is not yet verified on a real host.
- Idle budget: the service does not initialise `internal/install`. The budget
  benchmarks do not measure this -- they measure the running service. What is
  measured is what linking it costs:
  `agent/internal/install/imports_test.go` pins its imports under
  `GOOS=windows` and `TestInstallLinksNothingTheServiceDoesNotAlreadyLink`
  requires its dependency closure to be linked by the rest of the service
  already; `agent/internal/capabilities/uninstall/imports_test.go` does the
  same for `agent.uninstall` on Linux, Windows and macOS; `dormant_test.go`
  holds `internal/install` to having no `init()` and no package-level variable
  initialised by a call.

**Part 2**

- In CI on Linux: a Samba AD domain controller container plus a test WinRM
  server that validates SPNEGO with `gokrb5`. The push authenticates with
  Kerberos and runs the fixed steps.
- The client **refuses** when the server offers only NTLM, only Basic, or asks
  for CredSSP.
- A target with no DNS name is refused before the credential is decrypted.
- The job parameters contain no command text (schema test, as for SSH).
- `_device_os_arch` accepts Windows; a Windows target with transport `ssh` is
  refused and vice versa.

**What CI cannot prove:** a push into a real Windows domain. The Windows push
row carries *Not yet tested* until the operator's first real push; the PR
includes a one-PC trial procedure.

---

## 6. Build order

1. **PR 1 — install link.** Settings + table + migration; page, download and
   `install.sh`; `/enroll/v1` link path; `review_state` + `JobEngine.create`
   gate; Devices review UI; `agent.uninstall`; the Windows self-installer;
   agent 1.6.0. Docs: packaging README, `spec.md` §2 and §7 amended.
2. **PR 2 — Windows push.** Windows accepted in dispatch; WinRM transport in
   `pushinstall`; Kerberos-only client; probe; credential type; readiness
   checks; `deploy/WINDOWS-PUSH.md`. Docs: `spec-push-install.md` D10
   overturned with §2.3 as its accounting.

Each PR ships with its migration (forward-only), tests, and the full deploy
sequence for the operator. PR 1 adds one `.env` key,
`ITS_TRUSTED_PROXY_CIDRS`, with a default that is correct for the shipped
compose file; the deploy sequence says so explicitly.

---

## 7. Out of scope

- Importing computers from Active Directory as vouched asset rows. Pushing to
  a PC still needs it to be an asset a person vouched for (2a(d)); bulk
  vouching is a separate decision because it widens 2a(d)'s allowlist.
- A self-service link for employees (Q2 chose IT staff).
- MSI packaging and Intune/GPO changes — the managed-deployment path stays as
  it is.
- Authenticode and Apple signing — separate, waiting on certificates.
