# Authenticode signing: what it would take, and what it would not fix

**Status: not built, and not this session's to build.** A signing key is the
operator's to obtain — this is a procurement decision with an engineering
tail, written so it can be *decided* rather than re-derived. It is the top item
on `spec.md` §8 as of 2026-08-26.

**Why it moved to the top.** Nothing about the binary changed; what changed is
how many machines it reaches. On the one-machine install path the endpoint
verifies the SHA-256 immediately before installing, so an unsigned binary has a
check standing in front of it. On the managed path
(`spec-managed-distribution.md` §4) **there is no install-time check at all**:
GPO and Intune copy a file from a distribution point and run it, as SYSTEM, on
every machine in scope. What stands between an attacker who can write to that
share and the whole fleet is the share's ACL, and — because the binary carries
no signature — that ACL is not one control among several. It is the only one.

---

## 1. What the operator must obtain

Windows will not accept a signature from a certificate this project can mint.
It has to come from a CA in the Microsoft Trusted Root Program, and since
**1 June 2023** the CA/Browser Forum baseline requirements say the private key
for *any* publicly-trusted code-signing certificate — OV as well as EV — must
be generated and held on hardware meeting FIPS 140-2 Level 2 or Common Criteria
EAL4+. **There is no longer a `.pfx` file to put in a CI secret**, which is
what the current `agent/packaging/README.md` signing table still assumes when
it names `WINDOWS_SIGNING_CERT` (base64 PFX). That table needs correcting
whichever option is chosen.

Three ways to satisfy that, and they are genuinely different decisions:

| | **A. Azure Trusted Signing** | **B. CA hosted signing service** | **C. Own HSM / cloud HSM** |
|---|---|---|---|
| What it is | Microsoft's managed signing service; certificates are short-lived and minted per signing request | DigiCert KeyLocker, SSL.com eSigner, Sectigo — the CA holds the key in its HSM and you call it | Azure Key Vault Managed HSM / AWS CloudHSM holding a key, with a certificate issued against it |
| Cost, order of magnitude | tens of dollars a year | hundreds of dollars a year, plus per-signature or per-seat fees | HSM running cost, plus the certificate |
| Identity validation | organisation validation; **public trust requires a legal entity 3+ years old**, otherwise only the private-trust flavour, which is useless here | OV or EV organisation validation | OV or EV organisation validation |
| Key custody | Microsoft | the CA | **you** |
| CI story | a service principal and a `dotnet`/`signtool` extension; no key in the pipeline | a vendor CLI plus credentials in the pipeline | PKCS#11 through `osslsigncode`, or `signtool` with a KSP |
| The catch | you do not hold the key, and eligibility is a hard gate on company age | you do not hold the key, and per-signature pricing turns CI into a meter | you hold everything, including the operational burden of an HSM |

Two properties worth deciding on explicitly, because they are easy to discover
late:

- **EV versus OV is about SmartScreen, not about cryptography.** An EV
  certificate earns Microsoft SmartScreen reputation immediately; an OV one
  accumulates it over installs, so early installs of an OV-signed binary can
  still get an "unrecognised app" interstitial. For an agent pushed by GPO to
  domain machines this matters little (SmartScreen assesses files carrying a
  mark-of-the-web, and a file SYSTEM copies from SYSVOL does not have one). For
  anything an administrator downloads and runs by hand, it matters.
- **Company age gates option A.** Public trust through Azure Trusted Signing
  requires a verified organisation that has existed for three years or more. If
  Toeshee does not clear that, A is off the table for a shipping product and
  the decision is between B and C.

**Timestamping is not optional and is free.** A signature without a countersigned
timestamp stops validating the day the certificate expires — every binary in the
field, at once. `signtool`'s `/tr <rfc3161-url> /td SHA256` is the whole cost.

---

## 2. Where the key lives

**Not in this repository, and not in a file** — `CLAUDE.md`'s "secrets never in
code or git", plus the hardware requirement above, together mean the key is
never a thing this repo or its CI holds bytes of. What CI holds is an
*authorisation to sign*: a service principal (A), vendor API credentials (B), or
an HSM credential (C). That is a materially better property than the old plan
and it is worth stating as a benefit rather than a constraint.

**It must not be a credential the control plane holds.** This is the part that
matters beyond Windows installs, and `docs/modules/agent/open-questions.md`
OQ-3 already says why: an agent that downloads a binary over mTLS and checks a
hash the control plane supplied has made the control plane able to replace
root/SYSTEM code on every endpoint while vouching for itself. A signing
authorisation that lives in the same place as `ITS_OPENBAO_TOKEN` reintroduces
exactly that. Keep it in a release workflow with its own environment and its own
approvers, not in the deployment.

---

## 3. Where in the build or publish path it happens

**Before the upload, and this is the load-bearing detail.** Signing rewrites the
PE: it appends a certificate table and updates the header, so the SHA-256 of a
signed `its-agent-windows-amd64.exe` **is not** the SHA-256 of what `build.sh`
just emitted. Sign first, then take the digest, then upload. Signing after
publish makes every digest in `deploy/README.md` wrong and every staging
comparison fail.

Today's path:

```
build.sh  ->  agent/dist/its-agent-windows-amd64.exe  ->  sha256sum  ->  POST .../artifact
```

With signing:

```
build.sh  ->  sign  ->  verify  ->  sha256sum  ->  POST .../artifact  (signed: true)
```

`build.sh` itself is the wrong home for it. It cross-compiles six targets on
Linux with no external toolchain, deliberately, and `signtool` is Windows-only.
Three ways to add the step without breaking that:

1. **A release workflow with a `windows-latest` job** that takes the artefact
   from the Linux build job, signs it, and re-publishes it. Most conventional;
   costs a second runner and an artefact hand-off.
2. **`osslsigncode` on Linux**, which speaks PKCS#11 and therefore works
   against a cloud HSM (option C) or a vendor PKCS#11 module. Keeps everything
   on one runner. Less well-trodden.
3. **The vendor's own CLI** (option A or B), which is usually cross-platform.
   Simplest when the vendor is already chosen.

**Whichever it is, it signs the two `windows/*` artefacts and nothing else.**
Do not build a generic "sign every artefact" step: `build.sh`'s own header says
it, and `machosign_test.go` enforces it — **any post-link rewrite of a
`darwin/arm64` binary invalidates the ad-hoc signature the Apple Silicon kernel
requires at `exec`**, leaving `LC_CODE_SIGNATURE` in place and every page hash
wrong, which macOS rejects exactly as hard as no signature at all.

Verify in the same job, because a signing step that silently no-ops is the
failure mode:

```powershell
signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 its-agent-windows-amd64.exe
signtool verify /pa /v its-agent-windows-amd64.exe
Get-AuthenticodeSignature .\its-agent-windows-amd64.exe | Format-List Status, SignerCertificate
```

**This is entangled with `spec.md` §8 item 1 ("who uploads artefacts?") and the
entanglement is an argument for doing both at once.** A workflow that signs is
the natural place to publish — fewer hands on the bytes, and the digest the
console shows becomes the digest of a signed file by construction rather than
by an operator remembering the order. Doing them separately means an operator
downloading a signed artefact from CI and re-uploading it by hand, which is the
same manual step with one more place to get it wrong.

---

## 4. What changes for the SHA-256 comparison

Nothing is removed. One thing is added, and it is a better *kind* of check.

| | Today | With a signature |
|---|---|---|
| **Publish** | operator compares their local `sha256sum` against what the console computed | unchanged, over the **signed** bytes |
| **Stage** | operator runs `Get-FileHash` on the file they downloaded, before it reaches the share | unchanged, **plus** `Get-AuthenticodeSignature`, which binds the file to a publisher instead of to a number the operator has to carry |
| **Install** | **nobody** | **still nobody by default** — see §5.1. Windows does not check Authenticode on an arbitrary `.exe` a startup script runs |

Two consequences for this codebase:

- **`agent_releases.signed` stops being purely a claim, if we want it to.**
  Today `deploy/README.md` says plainly that `signed` is "a claim **you** are
  making, not something the platform verifies", and passing `true` for an
  unsigned binary "puts a false word on the operator's screen at the moment
  they are deciding whether to trust the download". Once signing exists, the
  upload route *could* parse the PE certificate table and set the flag itself —
  the same reasoning that made `sha256` server-computed rather than
  client-supplied (`spec.md` §4.2: "a client-supplied digest verifies nothing —
  it is the same party supplying the bytes"). That is a small follow-up and it
  is the one that makes the console's `signed` column mean something.
- **The console's integrity text is server-side and must move with it.**
  `backend/app/modules/agent_distribution/managed.py::_PUSHED_NOTES` currently
  ends *"This binary is unsigned ... so the share's ACL is not one control among
  several — it is the only one."* That sentence is asserted by
  `test_agent_managed_deployment.py` (falsifier 8), so it cannot drift out of
  the console — which also means signing the binary without editing it leaves
  the console lying, and the test will not catch that direction. Change both.

---

## 5. What it does **not** fix

This is the half a procurement decision usually gets wrong, so it is itemised.

### 5.1 It does not, by itself, make the endpoint check anything

**Windows does not verify Authenticode before running an executable.** A GPO
startup script that runs `install.ps1`, which runs a signed `its-agent.exe`,
verifies exactly as much as it does today: nothing. Signing creates the
*possibility* of a check; it does not create the check.

What creates it is a policy the estate already has to deploy:

- **WDAC (Windows Defender Application Control)** with a publisher rule, or
- **AppLocker** with a publisher condition, or
- at minimum, **PowerShell `AllSigned` execution policy** for the scripts (which
  would then require `install.ps1` and `its-agent-enroll.ps1` to be signed too —
  they are not, and are not in scope above).

**So the honest statement of the benefit is:** signing plus a publisher rule
makes the endpoint refuse a substituted binary. Signing alone makes the
substitution *detectable after the fact* by anybody who thinks to run
`Get-AuthenticodeSignature`, and gives the operator a second, publisher-bound
way to verify at the staging step. Both are real. Neither is "the endpoint
verifies before installing", and no wording should imply it.

### 5.2 It does not protect the distribution point

An attacker who can write to the share can still replace the binary with
anything they like — an unsigned binary, or one signed by somebody else.
Without §5.1's policy, Windows runs it. **The share's ACL remains a control you
must get right**, and the sentence in the runbook and in the console notes does
not come out; it gains a companion rather than a replacement.

### 5.3 It does not touch macOS or Linux

Three separate mechanisms, three separate procurements:

- **macOS** needs a **Developer ID** and notarization for distribution outside
  managed devices, which is item #2 in `agent/packaging/README.md`'s signing
  table and is open for commercialization. It is *not* a gate on an
  MDM-delivered rollout, because an MDM payload is not quarantined and so is
  not Gatekeeper-assessed. Separately, a Developer ID would let a **PPPC/TCC**
  profile pin the agent by **team identifier** rather than by path — with the
  ad-hoc signature it can only pin by path, so anything that replaces the file
  at that path inherits the grant.
- **Linux** wants a signed apt repository (`Release.gpg`), not a signed binary.
- The **ad-hoc darwin signature is not progress on any of this.** Ad-hoc means
  signed by *nobody*: it proves the bytes have not changed since they were
  linked and proves nothing about who produced them.

### 5.4 It does not enable self-update on its own — but it is the missing half

> **Superseded 2026-09-23.** Self-update no longer waits on signing. The
> operator approved it as a signed, SHA-256-pinned `agent.update` job
> (`spec-self-update.md`), because `script.run`/`app.deploy` already extend
> the same trust to the control plane. Signing, when it lands, is an **added**
> check against a publisher pin compiled into the running binary
> (`spec-self-update.md` §10). §2's rule, that the signing authorisation must
> not live with the control plane, is what gives that pin its value. The
> paragraph below is the earlier position.

`docs/05-agent-spec.md`'s self-update requires the agent to verify a code
signature against a key **the endpoint trusts and the control plane does not
hold**, which is OQ-3's gate and the reason self-update is not implementable
today. An Authenticode signature produced in a release workflow the control
plane cannot reach, verified on the endpoint against a pinned publisher, does
satisfy that — **for Windows**. It leaves macOS and Linux where they are, and it
only holds if §2's rule about where the signing authorisation lives is
respected. Signing is a necessary condition for self-update, not a sufficient
one, and **self-update must still not be built before it exists.**

### 5.5 It does not survive a compromised signing key by itself

A stolen key signs the attacker's binary as convincingly as ours, and every
endpoint with a publisher rule then trusts it. Revocation plus timestamping is
what bounds that: timestamped signatures made before the revocation date stay
valid, everything after it does not. This is an argument for option A or B's
short-lived certificates over a long-lived key you hold, and it cuts against
the intuition that holding your own key is safest.

### 5.6 It does not remove a single operator step

Publish-time and staging-time digest comparisons both stay. The runbook gets
*longer*, not shorter.

---

## 6. The decision, stated so it can be made

**Question:** which of A, B or C, and does the release workflow publish as well
as sign (`spec.md` §8 item 1)?

**Recommendation, offered rather than assumed:**

1. **Check option A's eligibility first**, because it is a yes/no that costs an
   afternoon and, if the answer is yes, it is the cheapest option by an order of
   magnitude and the one with the least key-handling risk.
2. **If A is out on company age, take B over C.** C's key custody sounds better
   and buys less than it looks: it trades a vendor's HSM for an HSM you now
   operate, and §5.5 says the long-lived key you hold is the worse failure mode.
3. **Sign and publish in the same workflow.** The extra hand-off is where the
   unsigned-artefact-with-a-signed-digest mistake lives.
4. **Do not treat signing as closing the managed-path integrity gap.** Budget
   for §5.1's publisher rule in the same conversation, or the honest console
   text barely changes.

**What lands in this repository when the decision is made**, so the engineering
tail is visible up front:

- a release workflow (`.github/workflows/`) with the signing step and a
  `signtool verify` gate;
- `agent/packaging/README.md`'s signing table corrected — the `WINDOWS_SIGNING_CERT`
  base64-PFX recipe has not been legal since June 2023;
- `agent/packaging/build.sh`'s header, which currently says Windows "needs
  signtool before a real rollout";
- `managed.py::_PUSHED_NOTES` and `_MANUAL_NOTES`, plus their assertions in
  `test_agent_managed_deployment.py` (falsifier 8);
- `deploy/README.md`'s step 2 (`signed` is a claim you are making) and step 5's
  integrity section;
- `spec.md` §7 and §8, and `spec-managed-distribution.md` §4's closing sentence;
- optionally, server-side verification of the `signed` flag on upload (§4).
