# Windows push install over WinRM — one-time setup and the one-PC trial

This is what a Windows domain has to have in place before the platform can push
its agent onto a workstation, and how to prove it on one computer before
rolling it out. It is the operator half of
`docs/superpowers/specs/2026-09-28-install-link-and-windows-push-design.md` §2;
the decision it rests on is D10 in
`docs/modules/agent-distribution/spec-push-install.md`, overturned 2026-09-28.

**How a Windows push works, in one paragraph.** The console never connects to
the workstation. A site server on the workstation's segment, running a
WinRM-capable agent, receives a signed job; it asks a domain controller for a
Kerberos ticket for `HTTP/<workstation's DNS name>` using the push account's
password, connects to the workstation's WinRM service with that ticket, and
runs a fixed set of steps compiled into the agent: download this platform's own
agent package, check its SHA-256, install the service, delete the staging
folder. No command text travels from the console; there is nothing to type
into.

**Kerberos only.** The agent offers Kerberos and nothing else. NTLM, Basic and
CredSSP are not compiled into it, so a workstation that offers only those is
refused with *"this computer only offered NTLM; pushes use Kerberos only"* —
never fallen back to. That is why a push needs the workstation's **DNS name**
(a ticket is issued for a name, not an address) and a **domain controller the
site server can reach**.

**Never unencrypted.** Both ports ship:

| Port | Channel | Needs |
|---|---|---|
| **5986** (HTTPS) — primary | TLS, with Kerberos bound to the TLS channel (channel binding, §4) | a machine certificate on each workstation (§2), and the CA it chains to trusted by the site server (§3) |
| **5985** (HTTP) | Kerberos message encryption of every request and reply (MS-WSMV `application/HTTP-SPNEGO-session-encrypted`) | nothing per machine |

A plaintext HTTP body is never sent on either. The push tries 5986 first and
uses 5985 only when 5986 does not answer. Both are proven in CI against a Samba
domain controller and an independent Python (`pyspnego`) WinRM acceptor — not
against this platform's own code — but **neither has been proven against a real
Windows domain until you run §6.** The console says *Windows push is not yet
tested in a real domain* until then.

---

## ⚠ Read this before choosing the push account: the Domain Admin warning

The operator's decision (2026-09-28, design §0 Q7) is that the push account may
be an existing **Domain Admin**. The console accepts that and carries a
**standing warning** on the Credentials page and in *Before a push can run*
rather than refusing it. What the warning means, plainly:

- **What Kerberos protects.** The password is used only against the domain
  controller. What reaches the workstation is a ticket only the real computer
  account can open, bound to `HTTP/<its name>`, which cannot be relayed to
  another service. A machine that answers at the address but is not that
  computer receives nothing it can use. That closes the capture-and-relay
  exposure that kept Windows out of push until now — for Kerberos, which is why
  NTLM is refused rather than allowed as a convenience.
- **What it does not protect.** The **site server holds the password in memory
  for the length of a push.** A site server compromised during a push is a
  compromised domain if the account is a Domain Admin. A dedicated account that
  is a local administrator on workstations only bounds that to workstations.
- **Kerberos does not make a compromised domain safe.** A machine holding a
  genuine computer account that answers for another computer's name can
  receive a ticket for its own name only — not the target's — but if the domain
  is already owned, none of this helps.

If you can, create a dedicated account instead (for example `CORP\its-push`),
put it in the local **Administrators** group of workstations only — by Group
Policy, *Computer Configuration → Preferences → Control Panel Settings → Local
Users and Groups*, or *Restricted Groups* — and deny it interactive and remote
desktop logon. The credential is dedicated either way: the platform refuses to
use a *Windows domain account* credential for remote control or provisioning,
and it is presented only on the address ranges it is bound to.

---

## 1. The Group Policy that enables WinRM

Create one GPO (for example **"ITMS — WinRM for push install"**) and link it to
the OU that holds the workstations — **a test OU with one computer in it first**
(§6). Every setting is under *Computer Configuration → Policies*.

### 1.1 The service and the listener

1. *Administrative Templates → Windows Components → Windows Remote Management
   (WinRM) → WinRM Service →* **Allow remote server management through WinRM**
   = **Enabled**. IPv4 filter `*`, IPv6 filter `*` (these are the *local*
   addresses the listener binds to; which *remote* addresses may connect is
   the firewall rule's job, §1.3). This creates the **5985** listener.
2. *Windows Settings → Security Settings → System Services →* **Windows Remote
   Management (WS-Management)** = **Automatic**.

This policy creates the HTTP listener only. The **5986** listener needs a
certificate and is created per machine by the startup script in §2.3.

### 1.2 Authentication and encryption: Kerberos on, everything else off

Same folder (*… → WinRM Service*):

| Setting | Value | Why |
|---|---|---|
| Allow Basic authentication | **Disabled** | the platform never sends Basic; nobody else should either |
| Allow CredSSP authentication | **Disabled** | CredSSP delegates the password to the target — the exposure Kerberos avoids |
| Allow unencrypted traffic | **Disabled** | the platform encrypts on 5985 itself; nothing needs this |
| Disallow Kerberos authentication | **Disabled** | Kerberos is the only method the platform uses |
| Disallow Negotiate authentication | **Disabled** (or Not configured) | the platform sends Kerberos inside the `Negotiate` header, so Negotiate must stay allowed |
| Specify channel binding token hardening level | **Enabled → Strict** | see §4 |

### 1.3 The firewall, on the domain profile only

*Windows Settings → Security Settings → Windows Defender Firewall with Advanced
Security → Inbound Rules:*

1. **New Rule → Predefined → Windows Remote Management.** Of the two rules it
   offers, keep only **Windows Remote Management (HTTP-In)** for the
   *Domain, Private* profiles. Open the created rule → *Advanced* → untick
   **Private** and **Public**, leaving **Domain** only.
2. **New Rule → Port → TCP 5986 → Allow**, profile **Domain** only, named
   **Windows Remote Management (HTTPS-In)**.
3. On both rules, *Scope → Remote IP address → These IP addresses*: the
   addresses of your **site servers**. Nothing else ever needs to reach these
   ports for push install.

### 1.4 The DNS name the ticket is for

Kerberos asks for a ticket for `HTTP/<fqdn>`. A domain member's computer
account already answers for it: Active Directory maps `HTTP/` to the
computer's `HOST/` name by default, so **no SPN has to be created**. The one
thing that breaks it is somebody having registered `HTTP/<fqdn>` on a
*different* account (typically an IIS application pool). Check from any domain
machine:

```powershell
setspn -Q HTTP/pc1.corp.example.com
```

`No such SPN found` (the `HOST/` mapping applies) or the computer account
itself is correct. A user or service account there means the ticket is for that
account, WinRM cannot open it, and the push fails — remove that SPN or push a
different machine.

---

## 2. WinRM HTTPS certificates from AD CS (for 5986)

Skip this section and §3 if you will run 5985 only. 5986 is the primary,
recommended channel.

### 2.1 A certificate template

On the issuing CA (or any machine with the AD CS tools), `certtmpl.msc`:

1. Right-click **Computer** → **Duplicate Template**.
2. *General:* name it **WinRM HTTPS**. Validity as your policy requires.
3. *Subject Name:* **Build from this Active Directory information**, subject
   name format **Common name**, and tick **DNS name** under *Include this
   information in alternate subject name*. The certificate must carry the
   workstation's FQDN — the name the ticket and the TLS check both use.
4. *Extensions → Application Policies:* must include **Server
   Authentication** (the Computer template already does).
5. *Security:* **Domain Computers** — **Read**, **Enroll**, **Autoenroll**.

Then `certsrv.msc` → *Certificate Templates* → **New → Certificate Template to
Issue** → **WinRM HTTPS**.

### 2.2 Auto-enrolment

In the same GPO: *Windows Settings → Security Settings → Public Key Policies →*
**Certificate Services Client – Auto-Enrollment** = **Enabled**, with **Renew
expired certificates, update pending certificates, and remove revoked
certificates** and **Update certificates that use certificate templates**
ticked.

### 2.3 The 5986 listener: a computer startup script

Group Policy has no setting that binds a certificate to a WinRM listener, so a
startup script does it on each boot and whenever the certificate renews. In the
same GPO: *Windows Settings → Scripts (Startup/Shutdown) → Startup →
PowerShell Scripts → Add*, with this file (for example
`winrm-https-listener.ps1`):

```powershell
# Binds the WinRM HTTPS (5986) listener to this computer's auto-enrolled
# Server Authentication certificate. Runs as SYSTEM at startup. Idempotent.
$ErrorActionPreference = 'Stop'
Start-Service WinRM
$fqdn = ("{0}.{1}" -f $env:COMPUTERNAME, (Get-CimInstance Win32_ComputerSystem).Domain).ToLower()
$cert = Get-ChildItem Cert:\LocalMachine\My |
  Where-Object {
    $_.EnhancedKeyUsageList.ObjectId -contains '1.3.6.1.5.5.7.3.1' -and
    ($_.DnsNameList.Unicode | ForEach-Object { $_.ToLower() }) -contains $fqdn -and
    $_.NotAfter -gt (Get-Date)
  } |
  Sort-Object NotAfter -Descending | Select-Object -First 1
if (-not $cert) { return }   # not enrolled yet; the next boot binds it
$listener = Get-WSManInstance -ResourceURI winrm/config/Listener -Enumerate |
  Where-Object { $_.Transport -eq 'HTTPS' -and $_.Address -eq '*' }
if ($listener -and $listener.CertificateThumbprint -eq $cert.Thumbprint) { return }
if ($listener) {
  Remove-WSManInstance -ResourceURI winrm/config/Listener -SelectorSet @{Address='*'; Transport='HTTPS'}
}
New-WSManInstance -ResourceURI winrm/config/Listener `
  -SelectorSet @{Address='*'; Transport='HTTPS'} `
  -ValueSet @{Hostname=$fqdn; CertificateThumbprint=$cert.Thumbprint} | Out-Null
```

This script is yours, distributed by your domain — it is not something the
platform sends. On a workstation, check the result with:

```powershell
winrm enumerate winrm/config/listener
```

Two listeners: `Transport = HTTP`, `Port = 5985`, and `Transport = HTTPS`,
`Port = 5986` with `Hostname` equal to the FQDN and a `CertificateThumbprint`.

---

## 3. Trusting the AD CS root on the site server (5986 only)

The site server checks the workstation's certificate like any TLS client, and
**never switches that check off.** A **Windows site server** that is a domain
member already trusts your enterprise CA (AD publishes it to every member). A
**Linux site server** does not, and a 5986 push from it fails the TLS check
until you give it the root.

1. **Export the root.** On the CA:

   ```powershell
   certutil -ca.cert adcs-root.cer
   certutil -encode adcs-root.cer adcs-root.pem
   ```

   `-ca.cert` writes the CA's own certificate. **If the WinRM certificates are
   issued by a subordinate (issuing) CA under an offline root**, export both —
   the root from any domain machine's `certlm.msc` → *Trusted Root
   Certification Authorities → Certificates* → your root → *All Tasks → Export →
   Base-64 encoded X.509 (.CER)* (that file is already PEM) — and put both in
   the bundle, root last. On a Linux machine, `openssl x509 -inform der -in
   adcs-root.cer -out adcs-root.pem` does the same conversion as
   `certutil -encode`.
2. **Check it from the site server** before touching the console (run on the
   site server, with `adcs-root.pem` copied there):

   ```sh
   read -rp "Workstation DNS name (FQDN): " WS_FQDN
   openssl s_client -connect "$WS_FQDN:5986" -servername "$WS_FQDN" -CAfile adcs-root.pem </dev/null 2>/dev/null | grep 'Verify return code'
   ```

   `Verify return code: 0 (ok)`. Anything else (`unable to get local issuer
   certificate`, `hostname mismatch`) will fail the push the same way — fix it
   here first.
3. **Add it to the credential.** In the console, **Credentials → the Windows
   domain account → CA certificate**, paste the whole PEM (every `-----BEGIN
   CERTIFICATE-----` block). The site server adds it to its trusted roots, *in
   addition to* the system roots, for that push only. It is not secret, but it
   travels inside the vaulted credential so that it always goes with the
   account it belongs to.

---

## 4. Channel binding (CBT / EPA) and the rest of the hardening

- **Channel binding on 5986.** The platform binds its Kerberos authenticator to
  the TLS channel it is sent over (RFC 5929 `tls-server-end-point`: a hash of
  the workstation's certificate, inside the Kerberos checksum). A machine in
  the middle that terminates TLS with its own certificate cannot forward the
  ticket to the real workstation, because the binding no longer matches. So
  the platform works with — and you should set — **Specify channel binding
  token hardening level = Strict** (§1.2). On 5985 there is no TLS channel to
  bind; the protection there is Kerberos mutual authentication plus message
  encryption.
- **Mutual authentication is required.** The agent verifies the workstation's
  Kerberos reply and refuses to continue if it is missing or does not verify.
- **The KDC.** The site server finds a domain controller by DNS SRV
  (`_kerberos._tcp.<your AD domain>`), so it must use a DNS server that answers
  for the AD domain, and reach a domain controller on **TCP and UDP 88**. If it
  cannot use AD DNS, set **Domain controller** on the credential
  (`dc1.corp.example.com` or `dc1.corp.example.com:88`).
- **Clocks.** Kerberos refuses a clock more than five minutes off the domain
  controller's. Keep the site server on NTP (`timedatectl` on Linux shows
  `System clock synchronized: yes`).
- **NTLM.** The platform never uses it, whatever the domain allows. Restricting
  NTLM domain-wide is your own hardening decision and does not affect push.

---

## 5. What the console needs

Checked by **Push install → *Before a push can run***, which now carries seven
items; the last two are for Windows:

1. A **Windows domain account** credential: the account as its **user
   principal name**, `user@corp.example.com`, and the password. `DOMAIN\user`
   is refused: the domain after the `@` is the Kerberos realm the push signs in
   to, the console shows it (`CORP.EXAMPLE.COM`) for you to confirm, and the
   domain controller that receives the account's password proof is the one you
   name under *Domain controller* or the one DNS names for **that** realm —
   never one found from a target's DNS name. The account is presented **only to
   machines whose DNS name is inside that domain** (`pc1.corp.example.com`, or
   deeper); a machine outside it is refused before the password is decrypted.
   Optional: *Domain controller*, *CA certificate* (§3).
2. That credential **bound to** the workstation's range (*Credential scopes*).
3. The range **authorized** for push install (*Push authorizations*).
4. An enabled **site server** serving the segment, whose agent is a
   **WinRM-capable release** (1.6.x or later). An older site server is skipped
   and the target is refused as having no WinRM-capable site server.
5. An agent package published for **Windows (64-bit Intel/AMD)** (or ARM).
6. **WinRM answers on the target** — from a probe (§6 step 8).
7. **A domain controller is reachable from the site server** — from the same
   probe. **Advisory:** the probe finds a domain controller by DNS for the
   target's domain; if it finds none, the console warns ("not found via DNS —
   fine if the credential names a domain controller") but still offers the
   install, because a credential that names its *Domain controller* never uses
   that lookup. Only WinRM answering (item 6) is required.

**`ITS_PUBLIC_CONSOLE_URL` must be set in `.env`** (the console's `https://` address, no path): a pushed computer is told to enroll there, and a push -- Windows, Linux or macOS -- is refused until it is set.

Per machine, the push also needs the asset row to be **one a person vouched
for** (added by hand, or promoted from *Devices → Discovered*; a row a sweep
recorded on its own is refused) and to carry three facts a machine that has
never run the agent cannot report, so a person types them:

- its **DNS name** — the full name, `pc1.corp.example.com`. Kerberos issues a
  ticket for a name, never an address. It must contain a dot, may not be an IP
  address, and is stored lower-case. It is **never** looked up from reverse DNS:
  that would let whoever answers PTR queries choose which computer your domain
  credential is presented to. A Windows target with no DNS name is refused
  before the credential is ever decrypted;
- its **operating system** (Windows) and **processor** (64-bit Intel/AMD or
  ARM): push picks the package from them, and the agent checks both on the
  machine before installing anything.

Set them on **Add asset** when you record the machine, or afterwards on the
machine's page (**Devices → the machine → *No agent on this asset* → Push
install details**). Both need permission to manage assets, and every change is
written to the audit log by field name. On a machine that already runs the
agent the three are the agent's to report, and a hand edit is refused.

---

## 6. The one-PC trial

CI cannot prove a push into a real Windows domain (design §5). This does, on
one computer you can walk up to, before the GPO is linked anywhere else.

**On the domain side**

1. Put one workstation (here `PC1`) in a test OU and link the GPO of §1–§2 to
   that OU only. On PC1, as an administrator:

   ```powershell
   gpupdate /force
   certutil -pulse
   Restart-Computer
   ```

2. After the restart, on PC1:

   ```powershell
   Get-Service WinRM
   winrm enumerate winrm/config/listener
   winrm get winrm/config/service
   ```

   `WinRM` is **Running**; two listeners (5985 and 5986, §2.3);
   `Basic = false`, `CredSSP = false`, `AllowUnencrypted = false`,
   `Kerberos = true`, `Negotiate = true`, `CbtHardeningLevel = Strict`.

**On the site server** (the one serving PC1's segment):

3. Name resolution, the KDC, the clock and the ports:

   ```sh
   read -rp "Workstation DNS name (FQDN): " WS_FQDN
   AD_DOMAIN="${WS_FQDN#*.}"
   getent hosts "$WS_FQDN"
   dig +short SRV "_kerberos._tcp.$AD_DOMAIN"
   timedatectl | grep 'synchronized'
   nc -vz -w 3 "$WS_FQDN" 5986
   nc -vz -w 3 "$WS_FQDN" 5985
   ```

   The name resolves to PC1's address; the SRV query lists at least one domain
   controller on port 88 (check it with `nc -vz -w 3` on that host and port 88);
   the clock is synchronized; both ports connect. Then the TLS check of §3 step
   2, which must print `Verify return code: 0 (ok)`. (`dig` is in
   `dnsutils`/`bind-utils`, `nc` in `netcat-openbsd`/`nmap-ncat`.)

**In the console**

4. **Set up §5 items 1–5** for PC1's address — for the trial, bind and
   authorize **PC1's single address** (a `/32`), not its subnet.
5. **Record PC1 as a push target.** If PC1 is not in **Devices** yet, add it
   with **Add asset**: Name `PC1`, IP address, **DNS name**
   `pc1.<your AD domain>`, Operating system **Windows**, Processor **64-bit
   Intel/AMD**. If it is (promoted from *Discovered*), open it and use **Push
   install details** to set the same three. The DNS name must be the one
   §1.4's `setspn` check and step 3's `getent` agree on.
6. **Tick it and start the push.** On **Devices**, tick **PC1** — the tick
   appears only for someone who may run a push, and only on a computer with no
   agent, an address and a person behind the row. Click **Push install the
   agent… (1)**. The panel that opens lists PC1 with its OS, processor and DNS
   name; if any is missing it says which and links to the machine instead of
   going on.
7. **Choose the credential.** Only **Windows domain account** credentials are
   offered for a Windows machine. The pre-flight opens beneath: PC1's address,
   the credential by name with the standing Domain Admin warning, the
   *cannot be verified* sentence and the lockout warning.
8. **Probe.** Click **Probe the targets**. The probe presents **no**
   credential; it only checks what answers. If the server refuses, it says why
   for PC1 in plain words — no site server serves its segment, it is not
   vouched for, its range is not authorized, the credential is not bound to it,
   the site server's agent is too old for WinRM, or it has no DNS name — and
   nothing is created. Fix what it names and probe again.
9. **Read the probe.** When the probe job has run (a few seconds; reopen the
   panel or reload if it still says *not probed yet*), PC1's Windows row shows
   **HTTPS (5986): answered** (or at least **HTTP (5985): answered**) and
   **Domain controller: reachable**, and its status reads **ready (Kerberos)**.
   There is no host key to confirm for Windows: Kerberos authenticates the
   machine. *Before a push can run* on **Push install** also shows **WinRM
   answers on the target** and **A domain controller is reachable from the site
   server** as *Done*. If it says **WinRM did not answer**, go back to §1; if
   **no domain controller reachable**, to §4 and step 3.
10. **Install.** Click **Push install to 1 machine**. The panel shows the
    dispatched batch; **Jobs** shows the install job on the site server, which
    should end **Succeeded**. If it fails with *"this computer only offered
    NTLM"*, Kerberos was not accepted — recheck §1.2 and §1.4. If it fails
    with *"%ProgramData%\ITSuite on the target is owned by an account other
    than Administrators or SYSTEM"*, someone created that folder before the
    agent was installed. Nothing was changed. Find out who and why, delete the
    folder as an administrator, and push again.

**Verify**

11. On PC1:

   ```powershell
   Get-Service its-agent
   Test-Path "$env:ProgramData\ITSuite\push"
   ```

   `its-agent` is **Running**, and the staging folder is gone (`False`).
12. In the console, PC1 appears in **Devices** as an enrolled agent (approve it
   if it is waiting for review), and **Audit** shows `push.dispatched` and
   `push.result` for the install.
13. **Try 5985 as well**, because it ships too — on a second workstation, PC2,
    so PC1's enrolment is left alone. Put PC2 in the test OU and run step 1 on
    it; after its restart remove its HTTPS listener so only 5985 answers:

    ```powershell
    Remove-WSManInstance -ResourceURI winrm/config/Listener -SelectorSet @{Address='*'; Transport='HTTPS'}
    ```

    Then steps 4–12 for PC2's address: its Windows row shows **HTTPS (5986): no answer**,
    **HTTP (5985): answered**, and the install succeeds over Kerberos message
    encryption. PC2's next restart puts the HTTPS listener back.

**Recording the result.** When steps 11–12 pass on 5986 (and step 13 on 5985),
Windows push has been tested in a real domain. Report it with the Windows
version of PC1 and the domain functional level; the change that records it
removes the *"Windows push is not yet tested in a real domain"* sentence from
the console (`frontend/src/modules/agent-push/PushReadiness.tsx`,
`WINDOWS_UNTESTED`). Then link the GPO to the next OU.

**If it fails**, the job result names the step; the common ones:

| Symptom | Cause |
|---|---|
| refused before dispatch: *no DNS name* | the asset has no DNS name: set it with **Push install details** (§5) |
| refused: *no WinRM-capable site server* | the site server's agent predates the WinRM release |
| probe: *no domain controller reachable* | site server DNS cannot resolve `_kerberos._tcp.<domain>`, or port 88 is blocked |
| install: Kerberos error *clock skew* | the site server's clock is more than five minutes off |
| install: *KDC_ERR_S_PRINCIPAL_UNKNOWN* | the DNS name is not the computer's AD name (§1.4) |
| install: TLS *unknown authority* | the CA bundle on the credential is missing or incomplete (§3) |
| install: *only offered NTLM* | Negotiate/Kerberos disabled, or the SPN is on another account (§1.2, §1.4) |
| install: HTTP 401 on 5986 with Strict CBT | a TLS-inspecting proxy sits between the site server and PC1 |

## 7. The Group Policy MSI (no WinRM, no push credential)

A domain can install the agent without WinRM or a push credential at all:
Group Policy software installation runs an MSI as SYSTEM at each computer's
next startup. The MSI joins through the install link, so every computer lands
in **Devices → Waiting for review**.

1. **Agent packages → Install link**: the link is on, and its networks cover
   the computers' addresses. For a rollout of more than 50 computers, raise
   *Most computers per hour* for the day (up to 500); above it, computers wait
   and try again.
2. A stable, release-signed agent **1.7.0 or later** is published for the
   architecture, and both `ITS_AGENT_RELEASE_PUBLIC_KEY` and
   `ITS_PUBLIC_CONSOLE_URL` are set on the server (the download says so if
   either is not). The MSI's console address is `ITS_PUBLIC_CONSOLE_URL` and
   nothing else — never the address your browser used — because the file goes
   to every computer in the domain.
3. Paste the link (the one shown when it was generated) and click **Download
   MSI for Group Policy** (64-bit Intel or AMD, or ARM). Note the SHA-256 the
   console shows, and check it on the file: `Get-FileHash .\ITMS-Agent-<host>-amd64.msi`.
4. Copy the MSI to a share the computers' accounts can read — normally under
   `\\<domain>\SYSVOL\<domain>\` — whose write access is limited to the
   administrators who manage it. That ACL is the only thing protecting the
   file from then on.
5. **Group Policy Management → Computer Configuration → Policies → Software
   Settings → Software installation → New → Package**, choose the MSI by its
   UNC path, then **Assigned**. Link the GPO to one test OU first.
6. Restart one computer in the OU. At startup it installs the agent, joins, and
   appears in **Devices → Waiting for review**; `msi-enroll.json` is no longer in
   `C:\Program Files\ITSuite`.
7. Link the GPO to the rest, and after the next restarts click **Approve all**.

**When a computer does not install or join,** look on it at
`C:\ProgramData\ITSuite\msi.log` (as an administrator: the directory is
readable by Administrators and SYSTEM only). Each run of the MSI's install or
removal step adds one line — the time in UTC, `--msi-install` or
`--msi-uninstall`, and `ok` or `failed:` with the reason, for example
`2026-09-30T08:11:12Z --msi-install failed: msi-enroll.json does not hold an
install link code`. It never holds the link code. No file at all means the
step never ran: check the GPO's software installation events in the
computer's Application log (source *Application Management*) and that the
computer can read the share. `ok` with no machine in *Waiting for review*
means the install worked and joining did not: the link is off, the computer's
address is outside the link's networks, or the hourly limit was reached (it
tries again). A removal that succeeded leaves no `msi.log` — the directory
goes with everything else.

**Removing it:** in the GPO, *All Tasks → Remove → Immediately uninstall*. Each
computer removes the service, its credentials and its data directory at the
next startup. **Replacing it** after a new release or a regenerated link:
assign the new MSI in the same GPO with *Upgrade*; computers already enrolled
stay enrolled. **Anyone who can read SYSVOL can read the link inside the MSI**;
**Regenerate** it on the Install link panel to stop every copy from joining
more computers.
