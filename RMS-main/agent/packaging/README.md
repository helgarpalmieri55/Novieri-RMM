# Packaging the agent

How to build the agent, put it on an endpoint as a real service, check that it
is running, and take it off again.

**Ubuntu is the tested path.** The control plane deploys on Ubuntu Server and
the first endpoints under test are Linux, so the Debian package, the systemd
unit and the uninstall are the parts of this directory that have actually been
executed. Windows and macOS are built, cross-compiled in CI, and **have not been
run by anybody** — [What is untested](#what-is-untested-and-why) says exactly
what that leaves you exposed to.

One macOS property is checked rather than merely built, because on Apple
Silicon getting it wrong means the binary never starts at all: the `darwin/arm64`
artifact carries the ad-hoc code signature the kernel requires at `exec`, and
CI verifies it page hash by page hash. See [Code signing](#code-signing).

---

## The one thing to get right: the enrollment token

An enrollment token mints a device identity inside a tenant. It is a credential
(invariant #3), and everything in this directory is shaped around three places
it must never end up:

| Where | Why not |
|---|---|
| **argv** | `/proc/<pid>/cmdline` is world-readable for the life of the process, and your shell keeps it in history. Every install script here **refuses** `--token` and tells you what to use instead. |
| **A service definition** | `systemctl show its-agent` prints `Environment=` to anyone who can read the unit; `launchctl print` does the same for a plist. Both outlive the token by months. |
| **A distributable artifact** | A token inside a `.deb` is copied to file servers, baked into golden images and cached by apt. One image build and the whole fleet shares one token. The install link is the first recorded exception, and it puts a code in a file's NAME, never in its bytes — see the next section. Its Group Policy MSI is the second, and it does put the code in the bytes — see [The Group Policy MSI](#the-group-policy-msi-the-code-inside-a-file-on-sysvol-operator-decision-2026-09-30). |

What it does instead: the token goes into **one file, `0600`, root-owned, in the
data directory** — `/var/lib/its-agent/enroll-token` — which the agent reads once
and **deletes the moment it has exchanged it for a certificate**
(`internal/enroll`, asserted by `TestASuccessfulEnrollmentDeletesTheInstallersTokenFile`).
A rejected enrollment keeps it, so a typo in the URL does not cost you a token.

The file's mode is enforced by the agent, not just by the installer: a token
file that others can read makes the agent **refuse to start** with an error
naming the file. A *spent* token file with a bad mode does not — the token is
only consulted before enrollment, so a leftover cannot brick a working endpoint.

You give the token to an installer in one of two ways, never a third:

```sh
# environment
sudo ITS_AGENT_ENROLL_TOKEN=<token> its-agent-enroll --url https://its.example.com

# stdin (preferred in pipelines — never lands in an environment either)
printf %s "$TOKEN" | sudo its-agent-enroll --token-stdin --url https://its.example.com
```

---

## The install link: a code in a file name (operator decision, 2026-09-28)

`docs/superpowers/specs/2026-09-28-install-link-and-windows-push-design.md`
§0 item 1 records the operator overturning the third row above for one
purpose: a technician opens the organisation's **install link** on the
machine, and the agent installs itself with no token to paste.

**What changed, exactly.** The bytes did not: the Windows download is the
published artefact, byte-identical for every tenant, and its release signature
(and, later, Authenticode) stays valid. The **link code** and the **console's
address** ride in the downloaded file's **name**,
`ITMS-Agent-Setup_<host>[@<port>]_<code>.exe`, and on macOS and Linux in the
one-line `install.sh` the page shows. A link code is not an enrollment token:
it enrols a machine only while the link is on, only from a network the
operator listed, only under the hourly limit — and what it enrols is a machine
**Waiting for review**, which receives inventory, health monitoring and
certificate renewal and nothing else until a technician approves it.

**What did not change.**

- **argv (row 1), with one exception that is stated, not hidden.** The code is
  never an *argument*: its UAC re-launch carries only `--elevated-install`, the
  service is registered with no arguments, and `install.sh` hands the code to
  `install.sh --token-stdin` through the shell's own `printf`. But the Windows
  self-installer reads the code from its own file name, and that file name
  **is** its image path -- `argv[0]` -- for as long as the installer runs. What
  records process creation therefore records the code: see "What remains"
  below. So the rule this section keeps is "no *argument* carries a
  credential", not "no command line does".
- **A service definition (row 2).** The service's environment holds the data
  directory and the console URL, never the code. The code goes to the ordinary
  token file, SYSTEM and Administrators only (Windows) or `0600` (unix), and
  the agent deletes it the moment it has a certificate.

**What remains, stated plainly.**

1. The file stays in the **Downloads folder** of every machine installed this
   way, name and all, until someone deletes it.
2. The file name is signed into the object store's presigned URL
   (`response-content-disposition`), so it appears in the
   **object-store access log** if that log is enabled.
3. While the installer runs, the code is in its own image path — `argv[0]` —
   because that is the file's name. No long-lived process ever carries it, but
   a short-lived one is enough to be recorded: **Security event 4688** (process
   creation) records the image path, and with command-line auditing enabled
   the whole command line; **Sysmon event 1** records both; and anything an
   **EDR agent or a SIEM** forwards off the machine takes the code with it, to
   wherever those logs are kept and for as long as they are kept. Beside the
   Downloads folder, this is the other place a copy of the code outlives the
   install.
4. Every refusal returns the same 404 body and headers, but **not in the same
   time**. A code that matches an organisation's link is looked up in a second
   step (the organisation's own row: on, not past its switch-off, source in a
   listed network) that an invalid code never reaches, so response timing can
   tell a real code from an invalid one. That is useful only to someone who
   already holds a leaked code and is **outside the allowed networks**: it
   confirms the code is real, not that it works from where they are. It does
   not help anyone find a code: codes are 32 characters from a 64-character
   alphabet, 192 bits, and cannot be guessed.

All four are bounded by the same controls (spec §1.6): the network list, the
hourly limit, the on/off switch, the optional automatic switch-off, and
**regeneration**, which makes every copy of the old code worthless at once.

**How the Windows installer knows which console to join.** From the file's
name, and from nothing else (operator decision D6a, 2026-09-28). The name is
parsed from the right: the code is the last 32 characters after a `_`; before
it sits the console's host, a DNS name (no IP literals, no underscores),
optionally followed by `@<port>` (1–65535). The enrollment URL is
`https://<host>[:<port>]`, with ordinary TLS verification. Because the name
carries it, a copy taken from a share or a USB stick installs too. A file
whose name does not parse is not an installer: it starts as a plain agent,
which has nothing to enroll with, exits non-zero, and installs nothing (the
Windows e2e runs exactly that case). The agent tolerates
a browser's ` (n)` / `(n)` suffix and any case of `.exe`.

The **Mark of the Web** (`ReferrerUrl` in `<file>:Zone.Identifier`) is **not
read**: it is not written by every browser, it is lost on a copy, and it is
removed by "unblock". The link page still sends `Referrer-Policy: origin`, for
its other purpose — keeping the code out of the object store's request logs.

**The host in the file name is unauthenticated, so a person confirms it.**
Anyone who can hand a technician a file can name it. Before anything is
elevated, the installer asks, in a Yes/No window, *"Install the ITMS agent and
connect this computer to &lt;host&gt;?"*; No exits and installs nothing. (The
Windows e2e sets `ITS_AGENT_INSTALLER_NO_UI=1`, which replaces every result
window with a line on stderr, and skips the question **only when the process
is already elevated**, as the e2e runner is; a double-click with the variable
planted in the user's environment is still asked. Nothing else sets it.) The
question is the control, and it is
only as good as the person reading it: a look-alike host a technician approves
still enrols the machine there -- into *that* console's review queue, running
only the published, signed agent (next paragraph).

**The copy that becomes the service is signature-verified.** The elevated
installer holds its own file open with `FILE_SHARE_READ` only (nobody can
write, rename or delete it while it runs) and copies from that handle, never
from the path again. Before the token is written or a service touched, it asks
the console for the release the link serves whose bytes are these
(`GET /i/<code>/windows.sig?arch=<amd64|arm64>&sha256=<hex>` → `{version,
sha256, signature}`, ordinary TLS, no redirects, the link's 404 rules; the
request format is frozen from 1.6.0 on), requires the staged bytes' SHA-256 to
be that release's, and requires the release to carry the offline release
signature under the key compiled into the binary --
`update.CheckReleaseSignature`, the check self-update makes. Anything else
fails closed: *"This file could not be confirmed as the ITMS agent the install
link serves"*. A saved copy of an older release (the link has since moved on
to a newer stable) still installs, as long as that release is still stable,
1.6.0 or later, and in the catalogue; the service then updates itself through
the ordinary rollout once approved. What this does **not** cover: the process UAC
starts is the file itself, so a file swapped *between* the double-click and the
UAC prompt runs elevated as whatever it now is -- UAC's own publisher line is
the check there. What it guarantees is that nothing but the published release
is installed as the SYSTEM service.

**Also on the elevated path.** DLLs resolve from System32 only
(`SetDefaultDllDirectories`, the first statement in both phases -- the file
sits in Downloads, next to whatever else was downloaded). The data directory is
owned by Administrators as well as ACL'd (an owner keeps `WRITE_DAC` whatever
the DACL says), a pre-existing data directory owned by anyone else is refused on
a fresh install, and the token file is created `O_EXCL` after removing any
file already at that name. `install.ps1` and `its-agent-enroll.ps1` do the same
(`SetOwner(BUILTIN\Administrators)`, `FileMode.CreateNew`).

**The data directory's parent too (2026-09-29).** Protecting `agent` alone
protected nothing: on a default Windows install `BUILTIN\Users` may create
folders in `C:\ProgramData` and `CREATOR OWNER` gets Full Control on them, so a
standard user could `mkdir C:\ProgramData\ITSuite` before the install, own it,
and -- owning it is `FILE_DELETE_CHILD` -- afterwards rename the protected
`agent` away and put their own in its place, with an `identity.json`,
`pins.json` and `agent.key` pointing the SYSTEM service at their gateway and
job-signing key. The self-installer, `install.ps1`, `its-agent-enroll.ps1` and
the uninstall helper now secure `%ProgramData%\ITSuite` **before** `agent` is
created or touched: created owned by Administrators with the protected
SYSTEM+Administrators DACL, or, if it exists, taken over (the Go side through
a handle that does not follow a link, `secdir.SecureOwned`); a junction or
symbolic link at `ITSuite` or `agent` is refused; and on a **fresh** install an
`ITSuite` or `agent` owned by anyone but Administrators, SYSTEM or the
installing administrator is refused and left alone -- taking it over would not
remove what its owner planted inside. The scripts lock down a parent only when
it is named `ITSuite`; a custom `-DataDir` elsewhere gets a warning instead.
`%ProgramFiles%\ITSuite` keeps its inherited ACL (`C:\Program Files` grants
Users read and execute only, so they cannot pre-create it) but is checked for
a link and, on a fresh install, for its owner. **Observed in CI:** the unit
tests on `windows-latest` (`agent-windows`) and `TestInstallLinkWindows`, which
reads `ITSuite` back and refuses an `ITSuite` owned by a local non-admin.
**Not covered:** an endpoint installed before this, where a user already owned
`ITSuite`, may already have had `agent` swapped; re-running the installer
secures the parent from then on but cannot tell a planted identity from a real
one.

**A link enrollment waits out "not now".** A 429 (the hourly limit), a 5xx or
an unreachable console is retried inside the agent on a capped (5 min),
jittered timer that honours `Retry-After`; a 404 or any other 4xx is final and
the token file is kept. A token enrollment keeps its single attempt.

**`agent.uninstall` (Reject), and what has been observed.** A signed job; the
agent answers `scheduled` and a detached helper removes the service, the
binary, the credentials and the data directory -- recursively only if the
directory holds an `identity.json`; otherwise only the named credentials go.
On Windows the helper runs from a copy staged in
`%ProgramData%\ITSuite\uninstall-<n>\`, created owned by Administrators and
SYSTEM+Administrators-only, and deleted with its directory at the next boot.
**Observed in CI:** Windows (`TestInstallLinkWindows`) and Linux under systemd
(`TestUninstallLinux`), both in `agent-update-e2e`. **Not yet verified: macOS.**
The launchd helper (`launchctl submit`, removed on every exit) has been
compiled and unit-tested against a fake service manager only; it has never
run on a Mac.

## The Group Policy MSI: the code inside a file on SYSVOL (operator decision, 2026-09-30)

`docs/superpowers/specs/2026-09-30-gpo-msi-and-subnet-push-design.md` Part 2
(the operator's answer to Q3) records a second exception to the token table's
third row: **Agent packages → Install link → Download MSI for Group Policy**
builds one MSI per organisation and link, and the MSI's **bytes** hold the link
code.

**What is in it, and what runs.** The published Windows agent — the server
re-hashes it against the catalogue and verifies its offline release signature
under `ITS_AGENT_RELEASE_PUBLIC_KEY` before wrapping it, and refuses otherwise —
and `msi-enroll.json`, `{"console": "https://<host>", "code": "<link code>"}`,
both into `%ProgramFiles%\ITSuite`. Windows Installer then runs the installed
binary as SYSTEM with one argument, `--msi-install`, never the code. It reads
the settings file, does what the self-installer does (secures
`%ProgramData%\ITSuite`, then `agent`; creates the token file `O_EXCL`;
registers and starts the service) and **deletes `msi-enroll.json`**; if it
cannot, the install fails and Windows Installer rolls back its files (what
the custom action itself did is not rolled back: item 6 below). On a computer that is already
enrolled it deletes the file, starts the service the MSI stopped, and changes
nothing else. Removal — **Remove** on the GPO, or `msiexec /x` — runs
`--msi-uninstall` first: the `agent.uninstall` removal (service, credentials,
data directory) without the binary, which Windows Installer removes itself. A
newer MSI for the same organisation upgrades the older one and never runs the
removal, so the machine stays enrolled. The MSI is built per request and never
stored; the audit record (`install_link.msi_downloaded`) holds the
architecture, the release and the MSI's SHA-256, never the code.

**Joining is the install link's, unchanged:** the allowed networks, the hourly
limit (raise it, up to 500, for a large rollout's day — computers above it are
told to try again), *Waiting for review* and **Approve all**, and the
`device.joined_via_link` event. Switching the link off or regenerating it stops
every MSI already handed out from joining more computers; computers that joined
are unaffected. The agent updates itself as usual: a new release does not need
a new MSI.

**What remains, stated plainly (spec §2.6).**

1. **Anyone who can read SYSVOL can take the code.** That is every domain user
   and computer, and they can read it from the MSI or its tables (`msiextract`
   is enough). The code only places a machine in the review queue. Revoking it
   is **Regenerate**.
2. **The share's permissions are the only integrity control.** Group Policy
   verifies no digest, and the MSI has no Authenticode signature ([What is
   still unsigned](#what-is-still-unsigned)). The console shows the SHA-256 of
   the file it just built — every download is a different file — so it can be
   checked once, before the file is copied to the share
   (`Get-FileHash .\ITMS-Agent-<host>-amd64.msi`).
3. **The code sits on disk briefly.** `msi-enroll.json` exists in
   `%ProgramFiles%\ITSuite` between Windows Installer copying the files and the
   custom action running, and the custom action deletes it. The directory is
   *writable* only by administrators, not readable only by them: a local user
   can read `msi-enroll.json` during that short window. That adds nothing
   beyond SYSVOL, where every domain user can already read the same code in
   the MSI (item 1).
4. **An MSI install over an agent that is already on the computer may replace
   its binary** with the MSI's release (Windows Installer's rules for files
   without a version resource decide); the agent's self-update brings it
   forward again.
5. **ARM64 is built, not observed.** wixl has no ARM64 target, so the ARM64
   MSI is built as x64 and marked `Arm64` in its summary information; CI
   installs only the x64 MSI (`TestMsiInstallWindows` in `agent-update-e2e`).
   Real Group Policy assignment at computer startup is not observable in CI
   either: `deploy/WINDOWS-PUSH.md` §7 is the operator's one-OU trial.
6. **A failed `--msi-install` is not rolled back by the MSI.** Windows
   Installer rolls back what it did itself — the files it copied — but the
   custom action has no rollback counterpart, so what it had already done
   stays: a registered `its-agent` service, or the token file in
   `%ProgramData%\ITSuite\agent`. Both are in the data directory only
   administrators and SYSTEM can reach, and the next attempt (the next
   startup, for an assigned GPO) recovers: it replaces the token file, and
   reconfigures and starts the service it finds. `%ProgramData%\ITSuite\msi.log`
   says why the attempt failed (`deploy/WINDOWS-PUSH.md` §7).

## Build

```sh
cd agent
./packaging/build.sh                 # all six targets into agent/dist/
./packaging/build.sh linux/amd64     # just one
VERSION=1.4.0 ./packaging/build.sh   # stamp it; unstamped builds report "dev"
```

Targets: `linux/amd64`, `linux/arm64`, `windows/amd64`, `windows/arm64`,
`darwin/amd64`, `darwin/arm64`. Every one is `CGO_ENABLED=0`, `-trimpath`, and
statically linked:

```
$ file dist/its-agent-linux-amd64
ELF 64-bit LSB executable, x86-64, ... statically linked, stripped
$ ldd dist/its-agent-linux-amd64
	not a dynamic executable
```

**This is why one package covers Ubuntu 24.04 and 26.04.** There is no glibc
version floor to skew: a dynamically linked agent built on 26.04 would fail at
exec on 24.04 with a `GLIBC_2.4x not found`, and that is the trap the static
build sidesteps. The agent's dependencies permit it — `github.com/coder/websocket`
is stdlib-only, and `golang.org/x/sys` is imported **only** from
`//go:build windows` files, so it is not even linked into the Linux binary.
`build-deb.sh` re-checks the "statically linked" property before packaging
rather than trusting it.

The Go toolchain is pinned at **1.24** (`agent/go.mod`), which is why
`golang.org/x/sys` is held at v0.33.0: v0.36.0 and later raise the module's `go`
directive, and v0.47.0 demands `go >= 1.25.0`.

### Getting the artefact to whoever installs it

`dist/` is on the build machine. Two ways it reaches an endpoint, and they suit
different sizes of job:

- **Publish it to the console.** Upload the binary once and the platform serves
  it back over a short-lived presigned link, with a **Platform → Agent
  Packages** screen that generates a one-machine install command carrying a
  single-use enrollment token. Best for onboarding a handful of machines by
  hand. The publishing steps are in `deploy/README.md`, "Publishing the agent
  to the console"; the platform computes the SHA-256 itself, so compare it
  against your local `sha256sum` before anyone installs from it.
- **Your own distribution.** For a fleet, keep using the `.deb` and your
  configuration management, and supply the token from a secret store as the
  section above describes. The console's install command carries its token as a
  literal, so it lands in the shell history of whoever pastes it — which is why
  that token is single-use and expires in hours. Acceptable for one machine;
  wrong as a rollout mechanism.
- **Managed distribution (GPO / Intune).** The console's **Managed deployment**
  action on the same page serves the artefact, the digest, the staging steps,
  and a **token-free** silent install — plus, as a separate act under a separate
  permission, a bounded multi-use deployment token. See
  [Managed distribution](#managed-distribution-gpo--intune) below.

Neither path pushes anything: **the platform never connects to an endpoint**
(invariant 2), so installing the agent is always an action taken on the machine
itself.

---

## Ubuntu / Debian — the package (recommended)

```sh
cd agent
VERSION=0.1.0 ./packaging/linux/build-deb.sh          # -> dist/its-agent_0.1.0_amd64.deb
ARCH=arm64 VERSION=0.1.0 ./packaging/linux/build-deb.sh
```

On the endpoint:

```sh
sudo apt install ./its-agent_0.1.0_amd64.deb
# installed, enabled, and deliberately NOT started: it is not enrolled yet
sudo ITS_AGENT_ENROLL_TOKEN=<token> its-agent-enroll --url https://its.example.com
```

The package installs `/usr/bin/its-agent`, `/usr/sbin/its-agent-enroll`,
`/usr/lib/systemd/system/its-agent.service` and this README. It **depends on
nothing** and carries no credentials. (`/usr/lib`, not `/lib`: Ubuntu 24.04 is
usr-merged and `base-files` diverts `/lib`, so a package shipping
`/lib/systemd/system` installs through a diversion.)

`postinst` does not start an unenrolled agent on purpose: an agent with neither
a token nor an identity exits 2 on every start, and `Restart=always` would turn
a forgotten enrollment step into a restart loop that reads like a crash.

| Operation | What happens |
|---|---|
| `apt install ./its-agent_*.deb` | binary, helper, unit; service enabled; started only if already enrolled |
| upgrade | `prerm` leaves it running, `postinst` restarts it — a restart, not a dark window |
| `apt remove its-agent` | service stopped and disabled, binary gone, **`/var/lib/its-agent` kept** so a reinstall resumes as the *same* device |
| `apt purge its-agent` | the above, plus the token, the private key, the identity and `/etc/its-agent` |

## Ubuntu / any systemd host — the shell installer

For a tarball rollout, or a host where you would rather not use dpkg:

```sh
sudo ITS_AGENT_ENROLL_TOKEN=<token> \
  agent/packaging/linux/install.sh --url https://its.example.com
```

It installs `/usr/local/bin/its-agent` (not `/usr/bin` — that is dpkg's), the
unit, `/etc/its-agent/agent.env` (0600), and the token file (0600), then
enables and starts the service. `--destdir DIR` stages the whole tree without
touching systemd, which is how it is tested in CI.

### Verify it is running

```sh
systemctl status its-agent
journalctl -u its-agent -f
test -f /var/lib/its-agent/identity.json && echo enrolled
test -f /var/lib/its-agent/enroll-token && echo "STILL HOLDING A TOKEN — not enrolled yet"
```

The first log line names the data directory and whether the endpoint is
enrolled. A rejected enrollment is a bare `403` by design: the control plane
does not say which check failed.

### Uninstall

```sh
sudo agent/packaging/linux/uninstall.sh              # removes state too
sudo agent/packaging/linux/uninstall.sh --keep-state # keeps the identity
```

**What every uninstall path here deliberately leaves behind**, because an
endpoint is not authorised to make fleet decisions about itself:

- **The device record in the control plane.** It keeps its history, inventory
  and audit trail until someone retires it in the console. A local uninstall
  deleting a server-side record would also make an *offline* endpoint
  indistinguishable from an uninstalled one.
- **The certificate at the gateway.** Deleting the local copy revokes nothing.
  Revoke in the console when decommissioning or after a compromise.
- **The audit log**, which is append-only and hash-chained (invariant #4).

Removing the state directory is irreversible in one specific way: the device key
and certificate are destroyed, so a later install needs a **new** enrollment
token and becomes a **new** device record.

---

## Why the service runs as root

`docs/05-agent-spec.md` ("Process & service model", and the per-OS packaging
table) fixes this: the agent runs as **root / SYSTEM** because the management
capabilities it exists to carry require it — patch installation, service
control, LVM snapshots for backup, provisioning.

Stated plainly, because "the spec says so" is not a security argument: **today's
compiled capability set is narrower than that.** Of what is built —
`inventory.collect`, `health.check`, `net.sweep`, the certificate capability —
exactly one privileged operation exists: `net.sweep`'s raw ICMP socket, which
needs `CAP_NET_RAW`. Everything else is reachable unprivileged or as uid 0
without any capability at all (`/sys/class/dmi/id/*` is `0400` **root-owned**, so
owner-read is enough; `/proc/<pid>/comm` is `0444`).

So the unit is root, and it is narrowed hard:

```
CapabilityBoundingSet=CAP_NET_RAW
AmbientCapabilities=CAP_NET_RAW
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/var/lib/its-agent
StateDirectory=its-agent   StateDirectoryMode=0700
ProtectHome=yes  PrivateTmp=yes  LockPersonality=yes  RestrictSUIDSGID=yes
RestrictNamespaces=yes  RestrictRealtime=yes  ProtectKernelTunables=yes
ProtectKernelModules=yes  ProtectControlGroups=yes  ProtectClock=yes
ProtectHostname=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK
```

That `CAP_NET_RAW` line is measured, not argued: the inventory, health and
netsweep test binaries all pass under `setpriv --bounding-set=-all,+net_raw`
(`CapBnd: 0000000000002000`), a raw ICMP socket opens there, and it fails
`EPERM` as soon as `cap_net_raw` leaves the bounding set. The **bounding** set is
the control that matters for a root service — at `execve` a uid-0 process takes
its permitted set from the bounding set, so narrowing `AmbientCapabilities`
alone would narrow nothing.

The bounding set is deliberately **too small for capabilities that are not built
yet**. `patch`, `backup`, `remote-control` and `provisioning` will each need
more, and each should widen that line with its own justification rather than the
line starting wide enough to hide them.

Two notes on directives the plan asked for differently:

- **`NoNewPrivileges=yes`**, where `docs/modules/agent/plan.md` Task 14 Step 1
  suggested `false` to protect the raw socket. That is a misreading: NNP blocks
  privilege *gain* through setuid and file capabilities on `execve`; it does not
  touch capabilities the manager granted at exec, so `CAP_NET_RAW` survives it.
  What it buys is that a compromised agent cannot escalate through a setuid
  binary — worth having on a process with fleet-wide reach.
- **`StartLimitIntervalSec` / `StartLimitBurst` live in `[Unit]`**, not
  `[Service]`. systemd moved them in v230; systemd 255 (Ubuntu 24.04) silently
  ignores them in `[Service]` and says so only under `systemd-analyze verify` —
  which is how the first draft's misplacement was found.

Directives deliberately **not** set, each with a reason, are listed in the unit
file itself (`ProtectProc`, `PrivateDevices`, `MemoryDenyWriteExecute`,
`SystemCallFilter`).

**No listening port, anywhere** (invariant #2): no `.socket` unit, no socket
activation, no `Sockets=` key in the launchd plist, and no firewall rule created
by the Windows installer. The agent dials outbound and only outbound.

---

## macOS (launchd) — UNEXECUTED, but deployable

Deployable via MDM: the arm64 binary carries the ad-hoc signature Apple Silicon
demands at `exec` (see [Code signing](#code-signing)), and an MDM-delivered
payload is not quarantined, so notarization is not a gate on this rollout. It
remains **unexecuted** — nobody here has a Mac.

```sh
sudo ITS_AGENT_ENROLL_TOKEN=<token> \
  agent/packaging/darwin/install.sh --url https://its.example.com
sudo launchctl print system/com.toeshee.itsagent   # verify
sudo agent/packaging/darwin/uninstall.sh
```

A system daemon in `/Library/LaunchDaemons`, `RunAtLoad` + `KeepAlive`, binary
at `/usr/local/libexec/its-agent`, logs at `/var/log/its-agent/agent.log`. The
data directory is `/var/lib/its-agent` — **not** `/Library/Application Support`
— because that is the agent binary's own default for every non-Windows platform
(`internal/config.defaultDataDir`), and an installer that disagrees with the
binary produces an agent that silently enrolls a second time into a second
device record.

## Windows (SCM) — UNEXECUTED

```powershell
$env:ITS_AGENT_ENROLL_TOKEN = '<token>'
.\install.ps1 -Url https://its.example.com
Get-Service its-agent
.\uninstall.ps1
```

**SCM integration is built, not merely wrapped.**
`agent/internal/service/service_windows.go` registers a real service control
dispatcher via `golang.org/x/sys/windows/svc`, handles `Stop`/`Shutdown` by
cancelling the agent's context and waiting for it to flush, answers
`Interrogate`, and reports an agent-initiated exit as a *service-specific* code.
`svc.IsWindowsService()` keeps the same binary usable from a console.

This was necessary rather than chosen: `docs/modules/agent/plan.md` Task 14 Step
2 preferred `sc.exe` over a plain console binary, and that does not work — a
program that never calls `StartServiceCtrlDispatcher` fails to start with error
**1053** every time. `golang.org/x/sys` is the agent's third dependency and is
imported only from `//go:build windows` files.

The installer sets the data-directory ACL to SYSTEM + Administrators (the NTFS
equivalent of the `0700` the agent enforces on unix), puts the URL in the
service's `Environment` registry value and the token in the token file, and
configures `sc.exe failure ... restart` as the counterpart of `Restart=always`.

---

## What was verified, and how

Executed in an Ubuntu 24.04 container, as part of building this:

| Check | Result |
|---|---|
| `go test ./... -count=1` (whole agent module) | pass |
| `shellcheck` on every `.sh`, on `its-agent-enroll`, and `-s sh` on the three maintainer scripts | clean |
| `systemd-analyze verify` on the unit | exit 0 (after it caught the `StartLimitIntervalSec` misplacement) |
| `install.sh --destdir /tmp/stage` | token `0600` root, state dir `0700`, env file `0600`, unit `0644`, binary `0755` |
| the token in any other staged file | zero occurrences |
| the real binary reading the installed token file | enrolls (fails at the unreachable URL, exit 3) — the token came from the file |
| the same with the token file at `0644` | refuses to start, exit 2, error names the file |
| `dpkg -i` → `its-agent-enroll` → `dpkg -r` → `dpkg -P` | binary at the package's version, token `0600` root, remove keeps state, purge destroys it and `/etc/its-agent` |
| `its-agent-enroll --token <secret>` | refused, with the reason |
| the deb-installed agent under the unit's environment | reads `ITS_AGENT_DATA_DIR` from `Environment=`, the URL from `EnvironmentFile=`, the token from the file, and enrolls |
| `file` / `ldd` on the linux/amd64 binary | statically linked, not a dynamic executable |
| the darwin/arm64 Mach-O's load commands | `LC_CODE_SIGNATURE` present; `CS_ADHOC` set; SHA-256; `codeLimit` reaches the signature; **all 1704 page hashes recomputed and matching**. Flipping one bit in the signed range is detected — the checker's own falsifier, run in CI |
| the darwin/amd64 Mach-O | no `LC_CODE_SIGNATURE`, and correctly so: the ad-hoc rule is arm64-only |
| `GOOS=windows go vet` + `go test -c` on `internal/service` | compiles |
| inventory / health / netsweep under `setpriv --bounding-set=-all,+net_raw` | pass; raw ICMP opens, and fails `EPERM` without `cap_net_raw` |

Three defects were found by executing rather than reading, and are fixed:
`StartLimitIntervalSec` sat in `[Service]` where systemd 255 ignores it; the
staging root inherited `mktemp -d`'s `0700`, which would have applied **mode
0700 to `/`** on install; and `build-deb.sh` reused whatever binary was already
in `dist/`, so `its-agent_0.1.0.deb` shipped a binary reporting
`2baebb0-dirty`. Each now has a check that would catch it again.

`agent/packaging/packaging_test.go` runs in `go test ./...` and is the standing
version of the structural half: it asserts the artifacts exist, that no service
definition or command line carries the token, that the unit's hardening
directives are present, that the plist is well-formed XML with one
`ProgramArguments` entry, and that every script is executable, strict-mode, and
free of `set -x`.

## What is untested, and why

**There is now a procedure for changing this, and it is the only thing that
will.** `docs/modules/agent-distribution/first-run-verification.md` is the exact
sequence to run once on one real Windows machine and one real Mac — every
command, what to observe at each step, and what each observation would falsify —
plus the four places that carry the "never executed" marker and must be updated
together when it comes off. Everything below stays true until somebody executes
it.


- **The Windows service has never run.** CI is `ubuntu-latest`; nothing here has
  a Windows host. The handler has a test
  (`internal/service/service_windows_test.go`) that CI **compiles** for
  windows/amd64 and **never executes**. What that leaves unproven: that the SCM
  accepts the dispatcher at all, that `New-Service` + the `Environment`
  registry value deliver the variables to the service, that the ACL calls
  behave, and that a stop is clean. Run the test suite on a Windows host before
  a fleet rollout.
- **The macOS daemon has never run.** The plist's structure is asserted; that
  `launchctl bootstrap` accepts it, that `PlistBuddy` edits it as expected, and
  that SIP/TCC do not object to a daemon reading system inventory are all
  unproven. **The binary has never been executed on a Mac by anybody on this
  project** — nobody here has one. The ad-hoc signature that Apple Silicon
  requires at `exec` is verified by arithmetic over the Mach-O (see [Code
  signing](#code-signing)), which is a strong statement about the file and not
  a substitute for having watched it start. Expect the first real finding on a
  Mac to be TCC, not signing.
- **Ubuntu 26.04 is reasoned about, not tested.** The static binary has no libc
  floor and the syscall ABI is stable, so it should be a non-event; the
  container this was built in is 24.04.
- **`systemctl` was never run against a live system here** — this container's
  systemd is offline (`systemctl is-system-running` → `offline`). The unit is
  verified by `systemd-analyze verify` and by staging, not by a running service.

## Found by reading, 2026-08-26

The Windows and macOS scripts were reviewed line by line against what they
claim to do — the exercise a script nobody has ever run is owed. Eleven defects
came out of it, all fixed here, and they are listed because **every one of them
is a defect that only reading could have found**: several fail on the second
install and never the first, one fails only on a machine where an earlier step
failed, and two were in documentation examples that would have produced a
silently wrong token.

| # | Where | What | Why nothing caught it |
|---|---|---|---|
| 1 | `windows/install.ps1` | `Copy-Item` replaced `its-agent.exe` **before** the service was stopped. Windows holds a section on a running image, so every upgrade-in-place died on a locked file — after the token had been written | first installs never hit it |
| 2 | `windows/install.ps1` | `-Token` was not declared, so PowerShell answered its own unknown-parameter error instead of the reason. Every other entry point refuses it with an explanation | the structural test knew this and did not check it |
| 3 | `windows/install.ps1` | `-TokenFromStdin` with empty stdin silently installed an unenrolled agent instead of failing | `[Console]::In.ReadLine()` returns `$null` at EOF and `if ($token)` is false |
| 4 | `windows/install.ps1` | a re-install without `-Url` **erased** `ITS_AGENT_ENROLL_URL` from the service `Environment`, stranding a staged machine with a token it could never spend | `New-ItemProperty -Force` replaces the whole MultiString |
| 5 | `windows/install.ps1` | the `.EXAMPLE` used `ConvertFrom-SecureString -AsPlainText`, which is **PowerShell 7.0+**. On Windows PowerShell 5.1 the same line *succeeds* and yields a DPAPI blob, so the token in the environment was an encrypted string and the control plane answered a bare 403 | it fails by succeeding |
| 6 | `windows/install.ps1` | `.DESCRIPTION` still said "and starts it", contradicting the §5.1 fix in the same file; the printed `Get-EventLog` advice is 5.1-only *and* names an event source nothing registers | prose is not executed |
| 7 | `windows/install.ps1` | `sc.exe failure`'s exit code was discarded — a native command's non-zero exit does not trip `ErrorActionPreference`, so a service with no recovery actions looked identical to one with them | |
| 8 | `windows/its-agent-enroll.ps1` | **the token was written to disk before the "is the service installed?" check**, so on a machine where `install.ps1` had never succeeded the deployment credential was left behind with nothing to spend it and nothing to remove it. On a GPO rollout that is the fleet token on every machine the install missed | the check existed; it was just last |
| 9 | `windows/its-agent-enroll.ps1` | `-DataDir` defaulted to this script's own default rather than the installed service's, so a mismatched install wrote the token where the service does not read — and the already-enrolled check then missed the identity and re-enrolled, minting a second device record | |
| 10 | `windows/*.ps1` | the documented `<token> \| .\script.ps1 -TokenFromStdin` form **does not work inside PowerShell at all**: a `.ps1` runs in the calling process, so the pipeline object goes to `$input` while `[Console]::In` stays attached to the console and the script waits for somebody to type. The redirect has to happen at the OS level | there is no PowerShell in CI to try it in |
| 11 | `darwin/install.sh` | it **bootstrapped an unenrolled daemon**. `KeepAlive` in the plist is unconditional and the agent exits 2 with neither identity nor token, so the Mac got the exact restart loop that falsifier 7 fixed for Windows — one exec every `ThrottleInterval`, forever. The plist's own comment claimed ThrottleInterval prevented this; it bounds the rate | the Windows fix was not carried across |

Two more, smaller, fixed with them:

- `darwin/install.sh` ran `launchctl enable` **after** `bootstrap`. A label in
  launchd's disabled database makes `bootstrap` fail, and under `set -e` the
  enable that would have cleared it never ran — so the install could never
  recover from one earlier `launchctl disable`.
- All three uninstallers (`linux/debian/postrm`, `darwin/uninstall.sh`,
  `windows/uninstall.ps1`) removed `enroll-token` and `agent.key` by name and
  relied on a recursive delete for the rest. `agent.key.pending` and
  `previous.json` are **also private key material** (`internal/enroll/rotate.go`:
  `previous.json {cert, chain, key}`), so a purge whose recursive remove failed
  left a device private key on the endpoint. All four are now named, and
  `packaging_test.go` anchors the list to `internal/config`'s own constants.
  `windows/uninstall.ps1` additionally claimed success unconditionally; it now
  says which of the two things it failed to remove.

**What was NOT found: any path that puts the enrollment token into argv.** It is
not in `New-Service -BinaryPathName`, not in the service `Environment` value,
not in `sc.exe failure`, not in `ProgramArguments`, not in the plist's
`EnvironmentVariables`, and not in any child process. Both PowerShell entry
points now declare `-Token` in order to refuse it, both Unix installers refuse
`--token`, and the token travels by environment variable or stdin to one
ACL-restricted file the agent deletes once spent. The residual is unchanged and
is stated where it applies: setting `$env:ITS_AGENT_ENROLL_TOKEN` **at an
interactive prompt** puts the value in PSReadLine's history, which is the
operator's shell rather than argv — `install.ps1`'s `.EXAMPLE` now shows the
`Read-Host -AsSecureString` form that avoids even that.

---

## Code signing

### macOS is three separate mechanisms, not one

This section used to say "nothing here is signed" and treat macOS as gated on
notarization. That was wrong in a way worth naming: it collapsed three
mechanisms with three different enforcement points, costs, and answers to "does
this block us".

| # | Mechanism | Enforced by | When | Cost | Status |
|---|---|---|---|---|---|
| 1 | **Ad-hoc signature** | the **kernel**, at `exec` | every launch, Apple Silicon only | free | **done — and asserted in CI** |
| 2 | **Developer ID + notarization** | **Gatekeeper** | first launch of a *quarantined* artifact | Apple Developer Program membership | open, for commercialization |
| 3 | **TCC / PPPC** | **TCC** | when the agent touches protected data | free; delivered by MDM profile | separate concern |

**#1 is the only absolute, and it is satisfied.** On Apple Silicon the kernel
kills an arm64 Mach-O that carries no code signature *at `exec`*. Not
Gatekeeper, not the quarantine attribute, not notarization, and not something
an MDM profile can waive — an unsigned arm64 binary simply does not start.

We do not trip it, and no third-party signing tool is needed to avoid it.
Cross-compiling `darwin/arm64` with `CGO_ENABLED=0` selects the Go **internal**
linker, which embeds an ad-hoc signature *itself*, in Go code, instead of
shelling out to the Mac-only `codesign(1)`. The binary this repo's Linux CI
builds therefore execs on Apple Silicon. Measured on what `build.sh` produces:

```
LC_CODE_SIGNATURE   54642 bytes at offset 6977968
CodeDirectory       version 0x20400  flags 0x20002 (CS_ADHOC | CS_LINKER_SIGNED)
                    SHA-256, 1704 page slots of 4096 bytes, codeLimit 6977968
                    all 1704 page hashes recomputed and matching
```

`agent/packaging/machosign_test.go` runs in `go test ./...` and re-derives every
one of those numbers from the artifact `build.sh` builds, so this is a checked
invariant rather than a toolchain default nobody chose. Two changes would
silently undo it and both go red there: a capability that **actually imports
`"C"`** (forcing external linking, which leaves darwin signing to the Mac-only
`codesign(1)`) and **any post-link rewrite** of the binary — `strip`, `upx`, an
appended payload — which leaves `LC_CODE_SIGNATURE` in place while invalidating
the page hashes, and macOS rejects that exactly as hard as no signature at all.
Do not add a `strip` step: `build.sh`'s `-s -w` already drops the symbol table
and DWARF *at link time*, which is the signature-safe way to the same size.

Note the first trigger precisely: it is cgo *use*, not the `CGO_ENABLED` flag.
Building with `CGO_ENABLED=1` today still produces a correctly signed binary,
because nothing imports `"C"` and the internal linker is picked anyway —
checked, not assumed. The flag looks like the control and is not, which is why
this is verified by measuring the artifact rather than by auditing build flags.

`darwin/amd64` carries no signature and needs none — the ad-hoc rule is
arm64-only and Intel Macs exec an unsigned Mach-O fine.

**#3 is where #2 still earns its keep.** A PPPC profile names the binary it
grants access to. Against an **ad-hoc-signed** binary it can only pin by
**path**, so anything that replaces the file at that path inherits the grant. A
**Developer ID** signature lets the profile pin to a **team identifier**
instead, which survives a path change and does not extend to a substitute
binary. That is a reason to want #2 eventually — not a reason it blocks today,
because a path-pinned profile works.

### What is still unsigned

| Platform | Where the step slots in | What it needs |
|---|---|---|
| Windows | after `build.sh`, before `build-deb.sh`'s equivalent MSI step: `signtool sign /fd sha256 /tr <timestamp-url> its-agent.exe` | an EV or OV code-signing certificate; CI secrets along the lines of `WINDOWS_SIGNING_CERT` (base64 PFX) and `WINDOWS_SIGNING_PASSWORD` |
| macOS (**#2 only**) | after `build.sh`: `codesign --sign "Developer ID Application: ..." --options runtime`, then `notarytool submit` and `stapler staple` on the `.pkg`. Needed for distribution **outside** managed devices, i.e. commercialization — an MDM-delivered payload is not quarantined and so is not Gatekeeper-assessed | an Apple Developer ID, `APPLE_SIGNING_IDENTITY`, `APPLE_TEAM_ID`, `APPLE_NOTARY_KEY` |
| Linux | `dpkg-sig` / a signed apt repository (`Release.gpg`) rather than the package itself | a repository signing key, `APT_SIGNING_KEY` |

**The keys are not in this repository and must not be** (`CLAUDE.md`: secrets
never in code or git). They belong in the CI secret store, and the signing steps
belong in a release workflow, not in a per-PR build.

> **The Windows row above is out of date in one specific way, and knowing which
> way saves a wasted procurement.** Since **1 June 2023** the CA/Browser Forum
> baseline requirements have required the private key of *any* publicly-trusted
> code-signing certificate — OV as well as EV — to live on FIPS 140-2 Level 2 /
> CC EAL4+ hardware. **There is no `.pfx` to base64 into `WINDOWS_SIGNING_CERT`
> any more.** What CI can hold is an authorisation to sign (a managed signing
> service, a CA-hosted key, or a cloud HSM credential), never the key.
>
> `docs/modules/agent-distribution/authenticode-signing.md` is the full
> write-up: the three ways to obtain a key and what each costs, where the
> signing step goes and why it must run **before** the digest is taken (signing
> rewrites the PE, so a signed binary's SHA-256 is not the build output's), and
> — at length — what signing does **not** fix. The headline of that last part:
> *Windows does not verify Authenticode before running an executable.* Signing
> creates the possibility of a check; a WDAC or AppLocker publisher rule creates
> the check.

**Self-update is now a signed job (decided 2026-09-23).** The operator
approved self-update as a signed `agent.update` job. The job is pinned to the
release SHA-256, the endpoint rolls back if the new version does not check in,
and releases roll out in rings. The design is
[`docs/modules/agent-distribution/spec-self-update.md`](../../docs/modules/agent-distribution/spec-self-update.md).
It is built (2026-09-23): `internal/capabilities/selfupdate`, `internal/update`
and the `agent-update-e2e` workflow, which is what says on which OS it has
actually run.

**The offline release-signing key.** Every self-update is also checked
against an Ed25519 signature the control plane cannot produce. The key:

- is created once, offline: `go run ./cmd/release-sign genkey > key.pem`;
- lives only in the GitHub secret **`AGENT_RELEASE_SIGNING_KEY`** (PKCS#8 PEM),
  read by `.github/workflows/release-agent.yml`, never committed and never
  given to the control plane;
- has its public half compiled in: `RELEASE_SIGNING_PUBKEY=$(go run
  ./cmd/release-sign pubkey) VERSION=1.4.0 ./packaging/build.sh`, which sets
  `internal/update.releaseSigningKey`.

`release-sign sign -version 1.4.0 dist/its-agent-<os>-<arch>` writes a `.sig`
beside each binary; it signs the version, platform and digest together, so a
signed binary cannot be relabelled. Upload each binary as today, then record
its signature with `PUT /api/v1/agent/releases/{id}/release-signature` (the
same `agent.release.manage` as the upload). The console does both from the
release workflow's draft release: **Agent Packages → Publish a release…** reads
its `SIGNATURES` file and records a signature only where the digest matches the
server's digest of the upload (`deploy/README.md`, "Publishing the agent to the
console"). An agent with a compiled-in key
refuses any update without a valid signature (`signature_required` /
`signature_invalid`); a build without one (a developer build) reports
`signature: not_required`. Release versions must be strict semver: a `dev` or
git-SHA build never self-updates.

*What this replaces.* Until 2026-09-23 this section refused self-update, and
[OQ-3](../../docs/modules/agent/open-questions.md#oq-3--the-agent-ships-without-a-developer-id-and-self-update-cannot-be-built-until-it-does-not)
recorded why. An agent that checks a hash supplied by the control plane lets
the control plane replace root/SYSTEM code while vouching for itself, so
self-update was to wait for a signature the control plane does not hold. The
2026-09-23 platform review (reviewer 3, §4) showed that this bought nothing.
`script.run` and `app.deploy` already let a signing control plane run arbitrary
code as SYSTEM/root, and `app.deploy` does it against exactly such a hash
(`internal/capabilities/deploy/script.go`, `install.go`). The refusal protected
nothing that was not already given away, and it left the fleet without an
upgrade path.

What self-update does add is an **availability** risk: a bad release that cuts
the management channel. The spec spends its effort there, on a local health
gate with rollback, a capped pilot ring, and a failure threshold.

*What still holds from the refusal.* The ad-hoc signature above is still not a
code-signing check: ad-hoc means *signed by nobody*. A certificate-backed
signature is still wanted. Once Authenticode exists, the agent will verify it
against a publisher pin **compiled into the running binary, never taken from
the job** (spec §10). That makes this path no weaker than the endpoint's own
trust. It does not make a compromised control plane harmless, and nothing on
this platform does.

---

## Managed distribution (GPO / Intune)

`docs/modules/agent-distribution/spec-managed-distribution.md` is the full
argument; this is the operator's half of it.

**Install and enrol are two steps here, and that is the whole design.** A GPO
software-distribution point is read by every machine account in the domain by
construction, so anything staged on it is public to the estate. The rule at the
top of this file — *a token must never reach a distributable artifact* — is at
its sharpest on this path, so:

1. **The package carries no credential.** Binary, `install.ps1`,
   `its-agent-enroll.ps1`. Nothing else, and nothing tenant-specific: the
   artefact is byte-identical for every organization.
2. **The install runs first and does not enrol.** `install.ps1` with no token
   registers the service and — since this cycle — **does not start it**. An
   agent with neither a token nor an identity exits 2 on every start, and the
   `sc.exe failure … restart` actions would turn that into a restart loop on
   every machine in the domain. `linux/debian/postinst` has always refused to
   start an unenrolled agent for exactly this reason; Windows now matches it.
3. **The enrol runs second, with the token from your own secret mechanism.**
   `its-agent-enroll.ps1` is the Windows counterpart of `its-agent-enroll`. It
   takes the token from `ITS_AGENT_ENROLL_TOKEN` or `-TokenFromStdin`,
   **refuses `-Token`**, writes it to one ACL-restricted file the agent deletes
   on success, and is a no-op on an endpoint that already has an identity —
   which matters, because a startup script runs at every boot.

```powershell
# staged on \\<dfs-or-sysvol>\its-agent\ , run by a computer startup script (SYSTEM)
.\install.ps1 -Binary .\its-agent-windows-amd64.exe -NoStart

$env:ITS_AGENT_ENROLL_TOKEN = <read from a location only machine accounts can read>
.\its-agent-enroll.ps1 -Url https://its.example.com
```

### The token for a fleet, and what it costs

A single-use token cannot serve a fleet: three hundred machines booting on the
same Monday would race for one use and 299 would get a bare 403. So the
managed credential is **multi-use and longer-lived**, and the console's
**deployment token** is that credential with the bounds made mandatory — a
finite `max_uses` you size to the rollout, and an expiry capped at fourteen
days. `POST /api/v1/enrollment/tokens` still defaults `max_uses` to *unlimited*;
do not mint a fleet token there.

**Whoever can read your distribution channel can enrol that many machines into
your organization until the token expires.** On the GPO path that means any
local administrator of any machine in scope, because a startup script runs as
SYSTEM. What they gain and what they do not is set out in full in the spec §3.4
and is shown on screen when you mint one — the short version is that a rogue
enrolment cannot forge a job, dispatch anything, or promote itself to a site
server, but it *is* a device somebody may later target.

Size `max_uses` to the machines you are actually deploying to. The use count on
the Enrollment page is how you would notice; slack in that number is precisely
what it cannot show you.

### What the digest comparison becomes

The one-machine install command verifies the digest **on the endpoint**, right
before installing. **GPO and Intune verify nothing** — they copy a file from a
distribution point and run it. So the check moves to you, and there are three
places rather than one:

| When | Who checks | What it establishes |
|---|---|---|
| Publish | you, against your build | the catalogue holds the bytes you built |
| **Stage** | **you, on the file you downloaded, before it reaches the share** | **the file the domain is about to run is the file the catalogue holds** |
| Install | *nobody* | — |

The console gives you the middle command (`Get-FileHash` / `sha256sum -c`).
After that, what protects the file is **the ACL on your distribution point**:
anyone who can write to that share can replace this binary and neither this
platform nor Windows will notice, because **the binary is unsigned** (see [Code
signing](#code-signing) — there is no Authenticode signature to fall back on).
Restrict write access to that share to the administrators who manage it.

### The MSI

Group Policy's *Software Installation* takes an MSI and nothing else. Since
2026-09-30 the console builds one — **Agent packages → Install link →
Download MSI for Group Policy** — and it joins computers through the install
link, not through a deployment token: it carries a link code, which places a
machine in *Waiting for review*, not a credential that mints identities without
one. [The Group Policy MSI](#the-group-policy-msi-the-code-inside-a-file-on-sysvol-operator-decision-2026-09-30)
says what it holds and what that costs. The startup-script path above stays for
estates that keep the install link off.

---

## Not built, and named rather than forgotten

- **A signed MSI, and an MSI with no link in it.** The Group Policy MSI exists
  ([above](#the-group-policy-msi-the-code-inside-a-file-on-sysvol-operator-decision-2026-09-30))
  but is not Authenticode-signed and works only with the install link on. An
  MSI for deployment tools that carries neither a code nor a token is not
  built; the script path covers them.
- **`.rpm`** (`build-rpm.sh` in the same list). The tarball path
  (`install.sh`) works on RHEL-family hosts; the package does not exist.
- **A `.pkg` for macOS.** Same shape: the script works, the double-clickable,
  MDM-deployable package does not.
- **Log rotation on macOS.** `/var/log/its-agent/agent.log` grows unbounded
  until a `newsyslog.d` entry exists.
