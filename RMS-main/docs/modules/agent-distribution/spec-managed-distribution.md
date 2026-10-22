# Managed distribution (GPO / Intune), and the enrolment credential it needs

**Status:** built, 2026-08-26, on the operator's ruling of the same date:
*build managed distribution as a first-class path, and build push install.*
This document is the first half. Push install is
`spec-push-install.md` §1–§9 and is not touched here.

**What the operator's ruling changed.** `spec-push-install.md` §1.2's second
recommendation — *"make third-party distribution first-class… For a
domain-joined Windows fleet, GPO software installation is a better answer than
anything in §3–§9, and it is available today"* — was a recommendation. It is
now a decision. §7.4 said the safe push transport (SSH) serves the minority of
Toeshee's fleet and that the Windows majority is better served by GPO; that
observation is what this document builds.

**What it does not change.** Not one word of invariant 2, and nothing here
opens a connection to an endpoint. Managed distribution is the *opposite* of a
push: the estate's own directory pulls, on its own schedule, under its own
audit. `spec.md` §1's refusal stands on its own terms, and
`tests/test_agent_distribution_opens_no_connection.py` still passes over this
module unchanged.

---

## 1. What a managed deployment actually consumes

An administrator setting up GPO software installation or an Intune Win32 app
needs four things, and today's console serves exactly one of them (the artefact
byte, behind a one-machine install command that is useless to them).

| # | What they need | Today |
|---|---|---|
| 1 | The **artefact**, downloadable once and stageable to a distribution point | Presigned GET exists, but only inside a token-bearing install command |
| 2 | A **silent, unattended install invocation** that installs the service and **does not enrol** | Does not exist. `install.ps1` takes a token or none, and **starts the service either way** — §5.1 |
| 3 | A **separate enrolment step** the distribution channel can run with a credential it supplies out of band | Exists on Linux (`its-agent-enroll`), **does not exist on Windows** |
| 4 | An **enrolment credential a fleet can share** without becoming a standing secret | **This is the hard part.** §3 |

This module builds 1–3 and answers 4 in the narrowest form that is defensible,
and it stops at a line it names (§3.6).

### 1.1 The package is token-free, and that is the load-bearing property

`spec.md` §2 and `agent/packaging/README.md` already forbid a token in a
distributable artefact, in as many words: *"A token inside a `.deb` is copied
to file servers, baked into golden images and cached by apt. One image build
and the whole fleet shares one token."*

A GPO software-installation package is the most distributable artefact this
platform will ever touch: it sits on SYSVOL or a DFS share that **every machine
account in the domain reads by design**. So:

> **The deployment package contains no credential of any kind, and the console
> route that produces it mints none.**

That is not a wording preference; it is the property that makes this path
cheaper than push install, and it is enforced by two different routes under two
different permissions:

- `GET /api/v1/agent/releases/{id}/deployment-package` — `agent.release.read`.
  Artefact, digest, staging steps, install invocation. **Writes nothing and
  mints nothing.**
- `POST /api/v1/agent/releases/{id}/deployment-token` — `enrollment.manage`.
  The credential, on its own, with its bounds stated. Never returned by the
  route above.

Falsifier 1 (§6) is that the first response contains no token and creates no
`enrollment_tokens` row.

---

## 2. What the console now serves

### 2.1 `GET …/deployment-package?channel=gpo|intune|manual`

Returns, for one published release:

- version, os, arch, size, **the SHA-256 the server computed**, `signed`
- a short-lived presigned download URL (the same 15-minute grant the
  one-machine command uses — an administrator downloads once)
- **staging steps**, per channel: download, verify the digest of the file *they
  now hold*, place it on the distribution point
- an **install invocation** that installs the service and **does not enrol**
- an **enrolment invocation** that takes the token from the environment or
  stdin, never argv
- the **integrity statement** of §4, including what is *not* checked

Audited as `agent.deployment_package_generated` (actor, release, channel). It
hands out a presigned read of the artefact, which `spec.md` §5 already treats as
worth a row.

### 2.2 `POST …/deployment-token`

Mints one bounded, multi-use enrolment credential. `max_uses` and
`expires_in_hours` are **both required and both bounded** — there is no
defaulting, because the two numbers *are* the blast radius and a default is the
platform choosing it on the operator's behalf. Returns the token once, its
bounds, and the residuals of §3.4 as text the console must show.

Audited as `agent.deployment_token_generated` (actor, release, token id,
max_uses, expiry), paired with the `enrollment.token_created` the mint itself
writes — the same pairing `spec.md` §5 requires of the one-machine path.

---

## 3. The enrolment credential — the actual question

### 3.1 Why the single-machine answer does not transfer

The one-machine flow is safe because of three properties that all fail at fleet
scale:

1. **Single-use.** Three hundred machines starting a GPO-installed service on
   the same Monday morning would race for one use; 299 of them get a bare 403
   and the operator has no way to tell that from a revoked token.
2. **Hours of life.** A GPO applies at next boot or next refresh; a laptop that
   was off for a fortnight enrols a fortnight late. A four-hour credential
   turns "deployment" into "deployment for whoever was powered on".
3. **A literal in a pasted command.** There is nobody pasting. The credential
   must be *fetched* by an unattended process.

So the managed credential must be **multi-use and longer-lived**. There is no
design in which it is not, and any document that claims otherwise has quietly
moved the problem into the distribution channel.

### 3.2 What it must not be

A **long-lived shared secret embedded in the package** — the thing the brief
names and rejects, correctly. On the GPO path the package is world-readable to
the domain by construction, so a token inside it is a permanent fleet-wide
enrolment credential with no expiry an administrator will ever remember to
rotate. `agent/packaging/README.md` already forbids exactly this and the
console must not reintroduce it from the other side.

An **unbounded token** is the same thing wearing a shorter name.
`EnrollmentToken.max_uses` is nullable and null means *unlimited*, and
`POST /api/v1/enrollment/tokens` **defaulted it to null**. A fleet token minted
through the existing screen with the field left alone was a permanent
credential. That default was the real defect this module worked around; **it was
closed on 2026-08-26** and §3.5 records both the original reasoning and what
became of it. The column stays nullable for rows already issued; no route mints
another.

### 3.3 What it is

> **A deployment token is an ordinary `EnrollmentToken` minted with a
> mandatorily finite `max_uses` and a mandatorily bounded expiry, delivered to
> the endpoint by the distribution channel's own secret mechanism, never by the
> package.**

Concretely, and every one of these is checked server-side:

| Bound | Value | Why that one |
|---|---|---|
| `max_uses` | **required**, 1 … 1000 | Sized to the rollout by the person doing it. Never null. 1000 is above Toeshee's 250–300 and below "a fleet nobody is counting". |
| `expires_in_hours` | **required**, 1 … 336 (14 days) | Long enough for a GPO cycle plus the laptop that was on holiday; short enough that a forgotten token dies. |
| Revocation | one act, existing screen | `POST /api/v1/enrollment/tokens/{id}/revoke` already exists and kills every remaining use at once. |
| Visibility | `use_count` on `/enrollment` | An operator can see a token being spent faster than machines are being deployed. **This is the detection mechanism**, and it is the reason `max_uses` must be *sized*, not maximal: a token with 1000 uses for a 250-machine rollout hides 750 enrolments in its slack. |
| Audit | every mint and every enrolment | Invariant 4, unchanged. |

This is deliberately **not a new credential type**. `docs/05-agent-spec.md`'s
per-OS packaging table already says the Windows agent is *"GPO/Intune
deployable; enrollment token as install param"* and that *"installers accept the
enrollment token and control-plane address as parameters for zero-touch
rollout"* — the primitive was always meant to serve this, and
`site_server/registry.py` already reasons from "`max_uses` is nullable, meaning
unlimited by default" as an existing property of the platform. What was missing
was not a type. It was a surface that makes the bounds mandatory and states what
they buy.

**One thing in that table this module contradicts on purpose.** "Enrollment
token as install param" is exactly what `agent/packaging/README.md` refuses:
argv is world-readable through `/proc/<pid>/cmdline`, and on Windows process
command lines land in Security event 4688. The token reaches the endpoint as a
file, in both the one-machine and the managed flow. §5.2.

### 3.4 Blast radius if the package is read — stated, not waved at

**If the package is read: nothing.** It carries no credential. That is §1.1 and
it is the whole reason this path is defensible.

**If the token is read** — from the GPO startup script, the SYSVOL file it
reads, or the Intune script body — the reader can enrol up to
`max_uses − use_count` machines into this organization until `expires_at`.
Each enrolment yields a device row and a device-bound certificate. Precisely:

*What the holder gains*

- **Device identities in the tenant, up to the remaining uses.** Each is a real
  agent from the gateway's point of view: it holds a certificate, it appears in
  the fleet list, and it submits inventory the console will believe.
- **Eligibility to be targeted.** A rogue device is a device an operator may
  later select. Any job type whose signed params carry a secret —
  `provisioning` mints and carries credentials today — delivers that secret to
  whatever answers as that device. **This is the largest single item on this
  list** and it is a property of *being enrolled*, not of how enrolment was
  obtained.
- **Denial of onboarding.** Spending the uses stops the real machines enrolling,
  and the failure at the endpoint is a bare 403 that looks exactly like a
  revoked token.
- **Inventory pollution**, which is cheap to do and expensive to unpick.

*What the holder does not gain*

- **It cannot forge a job.** Signing happens in OpenBao; invariant 1 is
  untouched, and an enrolled agent is a job *recipient*, never an originator.
- **It cannot read another device's data**, dispatch anything, or reach any
  API — a device certificate authenticates to the gateway, not to the console.
- **It cannot promote itself to a site server.** `site_server/registry.py`
  states this as its opening property: LAN-wide privilege is claimable only by
  a human holding `site.manage`, *specifically because* an enrollment token is
  broadly distributable.
- **It cannot mint another token**, extend this one, or survive its revocation.

*Who can read it, per channel* — and this is the part a console must say out
loud, because it differs by an order of magnitude:

| Channel | Who can read the token | Reading it needs |
|---|---|---|
| **GPO startup script / SYSVOL file** | Any process running as SYSTEM on any domain-joined machine in scope, i.e. **any local administrator of any machine in the deployment** | Local admin somewhere in the estate |
| **Intune script or Win32 app** | SYSTEM on the assigned device during execution; the payload is encrypted at rest in Intune | Local admin on an assigned device, or Intune admin |
| **Manual / configuration management** | Whatever the operator's secret store grants | Their own controls |

Neither channel makes the token *secret from the estate*. Both make it secret
from an unprivileged user and from outside the estate. **That is the honest
claim, and it is a materially weaker claim than the one-machine token's.** It is
also strictly better than what push install offers, where the credential at risk
is an administrative one that authenticates *to* machines rather than one that
enrols machines *into* a tenant — §3.7.

### 3.5 Why the existing `POST /enrollment/tokens` default was not changed *(superseded 2026-08-26 — it has been)*

**This section's conclusion no longer holds, and the paragraph below is kept
because its reasoning is what shaped the fix.** `POST /enrollment-tokens` now
**requires** `max_uses` and caps it at `services/enrollment.py::MAX_TOKEN_USES`
— the same 1000 as `MAX_DEPLOYMENT_USES`, held equal by a test rather than
imported, because the kernel must not depend on a module. There is no route on
this platform that mints an unbounded enrolment credential.

The original text, and what became of each of its three grounds:

> `max_uses` defaulting to null is a defect for this path and arguably for every
> path. It was left alone, and the reason is compatibility rather than agreement:
> that route is the kernel's, `site_server/registry.py` reasons from the nullable
> default as a stated property, and `docs/05-agent-spec.md` documents the token as
> broadly distributable. Changing a kernel default from "unlimited" to "one"
> breaks every existing caller silently and in the direction that looks like a
> revoked token at the endpoint.
>
> So this module adds a route that **cannot** express the unbounded case, rather
> than removing the ability to express it elsewhere. **Found and not fixed**, and
> recorded here so it is a decision rather than an oversight: the enrollment
> console still offers an unlimited-use token with no ceiling and no warning, and
> that screen is where an operator who never finds this page will go.

1. **"`site_server/registry.py` reasons from the nullable default."** It cited
   it, in a docstring, and read `max_uses` nowhere. Its conclusion — that "this
   host serves an entire subnet" must be claimable only by a human holding
   `site.manage` — never rested on the word *unlimited*: a token distributed to
   a domain by GPO is held by everyone the domain can read whether it enrols one
   machine or a thousand. The premise is corrected in place; the conclusion is
   untouched.

2. **"`docs/05-agent-spec.md` documents the token as broadly distributable."**
   It still is, and this module is the proof: a deployment token legitimately
   enrols a fleet. *Broadly distributable* and *unbounded* were being treated as
   one property. The cap separates them.

3. **"Breaks every existing caller silently, in the direction that looks like a
   revoked token at the endpoint."** **This one was correct, and it is the whole
   shape of the fix.** The silent, misdiagnosable failure it describes is what
   happens if the *stored* meaning changes — if `enroll()` re-reads a `NULL`
   `max_uses` as one. Every rejection in that guarded `UPDATE` is deliberately
   the same detail-free 403, so a fleet token that quietly became single-use
   presents to its operator exactly as one somebody revoked, on machines nobody
   is sitting at.

   So the stored meaning does **not** change. The refusal is moved to
   **creation**, where it is a 422 naming the field, read by a human on the
   screen that asked. `enroll()` still honours `max_uses IS NULL` as unlimited,
   for the rows already in the field, and a test pins that so a future change
   goes red here rather than on a fleet.

**No migration touches `enrollment_tokens`, and that is a decision.** A bound
this platform invented after the fact would either be a number no human chose
or — if it landed below the row's `use_count` — exactly the instant endpoint
rejection ground 3 warns about. Every such row is already time-limited
(`expires_at` is NOT NULL and was capped at 90 days at creation). What the
platform owes the operator is not a guess but **visibility**:
`GET /enrollment-tokens` returns `unlimited_count` — still-redeemable,
unbounded, org-wide, computed before paging — and takes `?unlimited_only=true`,
filtering on the same predicate it counts with. The enrollment console renders
the count as a callout with a *Show them* control, and revoking is what makes
the number fall.

### 3.6 The line this stops at — and what is on the other side

Two bounds would change the answer above from *"anyone who read the file"* to
something much smaller, and **neither is built here, because each is a new
credential semantics that needs its own spec**:

1. **Network-origin binding.** A token that only enrols from source addresses
   inside declared prefixes. This is not a filter, it is an *authorization
   input*, and getting it wrong is worse than not having it: the enrol endpoint
   sits behind a reverse proxy, so the "source address" is either the proxy's
   (useless) or an `X-Forwarded-For` header (**attacker-controlled unless the
   proxy chain is pinned**). A spoofable authorization that reads as a bound is
   the failure mode. It also has to answer NAT, VPN clients, site servers and
   roaming laptops — every one of which is a legitimate enrolment from an
   address the operator did not think of.
2. **Directory-vouched enrolment.** A token that only enrols a machine whose
   identity the directory already asserts — a Kerberos machine-account proof, or
   an Intune device attestation, presented alongside the token. This is the
   bound that would actually make a leaked GPO token near-worthless, because
   the reader would additionally need to *be* a machine the domain vouches for.
   It is also a **second trust anchor for device identity**, which is
   structurally the shape invariant 1b governs for MDM — disjoint identities,
   disjoint trust anchors, identity proven rather than asserted. It does not get
   invented in a build cycle for a distribution feature.

**Recommendation to the operator: (2) is the one worth specifying**, and it is
worth specifying *because* the console now makes fleet enrolment easy. (1)
looks cheaper and is a trap.

### 3.7 Against push install, on the same terms

Same estate, same 250–300 domain-joined Windows machines:

|  | Managed distribution | Push install (`spec-push-install.md`) |
|---|---|---|
| Credential the platform holds | An enrolment token for **its own tenant** | An **administrative credential for the estate** |
| Worst case if that credential leaks | N rogue devices in one tenant, until expiry, revocable in one act | Authenticated code execution on every machine it reaches; on Windows, relayable (§7.3) |
| Who authenticates whom | The endpoint authenticates the platform (mTLS, pinned CA) | **Neither** — the platform cannot authenticate an unenrolled target, ever (§1.1) |
| Reaches machines that are off | Yes, at next boot | No |
| New invariant needed | **None** | Amendment 2a |
| Audited by | The directory *and* this platform | This platform only |

That table is why §1.2 recommended this, and it holds after building it.

---

## 4. The integrity chain, for a package pushed by GPO

`spec.md` §4.4 rests the integrity story on one comparison: **the digest the
server computed, against the digest of the bytes the operator built.** On the
one-machine path that comparison is *also* re-run on the endpoint, by
`sha256sum -c` inside the pasted command, immediately before install.

**On the GPO path there is no such step, and no wording invents one.** GPO
software installation copies an MSI from a share and runs it; it verifies no
digest. The chain becomes three checks in three places, two of which are the
administrator's:

1. **At publish — unchanged, and still the anchor.** Build, `sha256sum` locally,
   upload, compare against what the console shows. This establishes *"the
   catalogue holds the bytes I built"*, and nothing downstream is worth anything
   without it.
2. **At staging — the new comparison, and the one this module adds.** The
   administrator downloads the artefact from the console **once**, and verifies
   the digest of the file **they now hold** against the one the console shows,
   *before* copying it to the distribution point. The console serves the exact
   command (`Get-FileHash` / `sha256sum -c`) so this is a paste, not a
   procedure. This establishes *"the file I am about to share with the whole
   domain is the file the catalogue holds"*.
3. **At install — there is no check, and the console says so.** Once the file is
   on the distribution point, what protects it is **the ACL on that share** and
   **a code signature on the binary**. The share is the administrator's. The
   signature **does not exist**: `agent/packaging/README.md` "What is still
   unsigned" — the Windows binary carries no Authenticode signature, and there
   is no certificate to sign it with.

So, stated as the console must state it:

> The digest comparison moves from *the endpoint verifies before installing* to
> *the administrator verified once, and the distribution point is
> write-restricted*. Anyone who can write to your distribution point can
> replace this binary, and neither this platform nor Windows will notice.
> Restrict write access to that share to the administrators who manage it.

That is a genuine reduction in the integrity guarantee, it is caused by the
deployment mechanism rather than by anything here, and the only thing that
would restore it is Authenticode signing — which is `spec.md` §8.2's open
question and needs the operator's certificate. **It is now the highest-value
item on that list**, because managed distribution is what makes the unsigned
binary reach three hundred machines instead of one.

---

## 5. The agent side

### 5.1 `install.ps1` started an unenrolled agent — fixed

A managed deployment installs first and enrols second, so an unenrolled
install is now a *normal* state rather than an operator error. `install.ps1`
started the service unconditionally and configured
`sc.exe failure … restart/5000/restart/15000/restart/60000`. An agent with no
token and no identity **exits 2 on every start**, so a GPO install with no token
would have produced a restart loop on every machine in the domain, reading in
the event log as a crashing service.

Linux already gets this right and says why: `linux/debian/postinst` deliberately
does not start an unenrolled agent, *"an agent with neither a token nor an
identity exits 2 on every start, and `Restart=always` would turn a forgotten
enrollment step into a restart loop that reads like a crash."* `install.ps1` now
follows it — it registers the service and starts it only when the endpoint has
a token to spend or an identity it already holds.

### 5.2 `its-agent-enroll.ps1` — new, and it is step 3 of §1

Windows had no counterpart to `linux/its-agent-enroll`, so the only way to
enrol a Windows endpoint was to hand the token to `install.ps1`. Managed
distribution needs the two steps separate: the package installs, the channel's
own secret mechanism enrols.

It follows the Linux helper exactly: token from `ITS_AGENT_ENROLL_TOKEN` or
`-TokenFromStdin`, **`-Token` refused with the reason**, the token written to
one ACL-restricted file the agent deletes on success, the URL in the service
environment and never the token, and a no-op with a clear message if the
endpoint is already enrolled.

### 5.3 No MSI, and the honest reason

`spec.md` §3 declined an MSI because WiX is an external toolchain dependency and
Windows code signing needs the operator's certificate. **That is still true and
this cycle does not change it.** What is served instead is a documented silent
invocation, which GPO can run as a startup script and Intune can wrap as a
Win32 app — both are first-class in those products.

What an MSI would additionally buy, stated so the gap is a decision: GPO's
*Software Installation* policy (the assign-and-forget one) takes an MSI and
nothing else. Without one, GPO deployment is a **startup script**, which is a
supported and widely used mechanism but a different one, with different
diagnostics. **Found and not fixed.**

---

## 6. Falsifiers

Each is a test.

1. **The deployment package carries a credential.** Assert the response
   contains no token, and that the call creates no `enrollment_tokens` row.
   *`test_agent_managed_deployment.py`*
2. **A deployment token can be minted unbounded.** Assert `max_uses` is
   required, that null is refused, that the ceiling is enforced, and that the
   stored row's `max_uses` is never null.
3. **Expiry is unbounded.** Assert the hours ceiling is enforced.
4. **The install or enrol invocation puts the token in argv.** The same rule
   `spec.md` §6.4 holds the one-machine command to, applied to both new
   strings — and to `its-agent-enroll.ps1`, which must refuse `-Token`.
5. **A route answers without a permission check.** `agent_releases` has no
   `org_id`; the existing completeness sweep must cover the new routes, and the
   package route must not require `enrollment.manage` while the token route
   must.
6. **The package is not tenant-independent.** Two orgs must get the same
   digest, the same install invocation and no org identifier in either.
7. **`install.ps1` starts an unenrolled agent.** §5.1 — a restart loop across a
   domain. *`agent/packaging/packaging_test.go`*
8. **The console claims install-time verification on the GPO path.** §4 — the
   integrity statement must say the check does not exist there. Asserted on the
   server-rendered text, so it cannot drift out of the console.

---

## 7. What this deliberately does not do

- **No push install.** `spec-push-install.md`, another cycle.
- **No MSI, no Authenticode signature.** §5.3, `spec.md` §8.2.
- **No origin-bound or directory-vouched enrolment credential.** §3.6 — named,
  recommended, and not invented here.
- ~~**No change to `POST /enrollment/tokens`.** §3.5.~~ **Superseded
  2026-08-26**: `max_uses` is now required and capped there too, on the
  reasoning in §3.5 as amended. This module's route is no longer the only one
  that cannot express the unbounded case — no route can.
- **No fetching of the token by the platform on the endpoint's behalf.** The
  console tells the administrator how their channel delivers it. A platform
  that reached into a key vault on an endpoint's behalf would be a connection
  to an endpoint.
