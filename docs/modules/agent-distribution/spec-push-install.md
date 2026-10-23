# Agent push-install, and the one journey from Devices — spec

**Status, as of 2026-08-26: PART A IS BUILT. PART B CYCLES B1 AND B2 ARE BUILT.
The feature now dispatches signed push probes and installs, under the four
bounds of invariant 2a, through a declared site server and no other path.**

**What B2 shipped**, item for item against §14's B2 list:

| §14 B2 item | Where |
|---|---|
| both job types (`agent.push_probe`, `agent.push_install`) | `backend/app/modules/agent_push/jobtypes.py`; both registered, `RESERVED_JOB_TYPES` emptied; both kernel gates still refuse them (clause (e)) |
| the `params_redactor` | `jobtypes.install_params_redactor` — strips `credential`, `enroll_token`, `release.url` from every `GET /jobs/{id}` |
| dispatch | `agent_push/dispatch.py` — the one file that signs a job and decrypts a credential; the refusal ladder is reused from `preflight`, never recopied; envelopes published after commit, exactly as `site.wake` |
| scrub-on-terminal | a kernel extension point `JobType.terminal_params_scrubber` applied at EVERY terminal writer in `app/jobs/engine.py`; `jobtypes.install_terminal_scrubber` removes the credential and token on any terminal state (§6.4). **Residual, not solution:** the plaintext lives in `jobs.params` for up to the 300s TTL; vault indirection for job params (the durable fix) is a kernel change out of scope |
| the Go capability | `agent/internal/capabilities/pushinstall/` — probe (no credential), install (verify confirmed host key, authenticate, verify OS/arch, verify artefact SHA-256 before transfer, place agent + 0600 token, zero credential); budget tests measured through `internal/budget` |
| the pre-flight screen | `frontend/src/modules/agent-push/PushPreflightPanel.tsx` |
| the enrolment correlation | the push mints a single-use ≤30-minute token named `push:{batch_id}:{address}`; `push.dispatched` records the token id + job id; **the `push.enrolled` back-correlation from the enrolment route is a one-line hook left for the enrolment module (that route is another cycle's file) — recorded below** |

**The host key is the crux, and it is stored, confirmed and verified.** A new
`push_host_key` table (migration 0080) holds what a credential-free probe
observed and what a human confirmed; an install is dispatched only against a
host whose confirmed key still equals the latest observation, and the confirmed
key is signed into the install so the agent's SSH client verifies against it.
A push to a host with no confirmed key, or whose key has changed, is refused —
not prompted, not warned about (§7.3, §4.4). The agent NEVER does
trust-on-first-use for an install and never accepts a host key blindly.

**Two carried-forward notes.** (1) `AgentPushInstallParams.credential` is on the
wire and in `jobs.params`; §6.4's residual is bounded by the redactor and the
terminal scrubber, not eliminated. (2) A `push_install` credential's vault secret
is a JSON object `{"username","secret"}`, because the `credentials` table stores
one opaque secret and no username column and adding one is not this module's to
make — the username travels inside the encrypted blob, decrypted only at
dispatch.

---

**(Below is the original spec, unchanged. B1's status notes are kept for the
reasoning around them.)**

**Status, as of 2026-08-26: PART A IS BUILT. PART B CYCLE B1 IS BUILT. Cycle B2
is not built, and nothing pushes anything.**

* **Part A — the one journey (§10) — is built**, at `/devices/onboard`, entered
  from the primary action on the Devices head and from the Fleet rail. It adds
  no privilege, no job type, no credential and no socket, exactly as §0
  promised: it deep-links into the five screens that already do this work and
  shows where the operator is and what is blocking the next step.
  `frontend/src/modules/onboarding/`.
* **§1.2's second recommendation — make third-party distribution first-class —
  was taken**, and is built as
  `docs/modules/agent-distribution/spec-managed-distribution.md`. That is the
  answer §7.4 said the Windows majority of the fleet deserves.
* **Part B — push install (§1–§9) — cycle B1 is built; cycle B2 is not.**
  Amendment 2a was approved by the operator on 2026-08-26 (§2.2), so §1.3's
  first precondition is met, and the binding text now lives in `CLAUDE.md`
  beside 1b — **that copy governs**; §2.2 below is the draft it was made from,
  kept for the reasoning around it. §14 required the two cycles "must not be
  merged", and they were not.

  **What B1 shipped**, item for item against §14:

  | §14 B1 item | Where |
  |---|---|
  | `push_authorization` | `backend/app/modules/agent_push/models.py`, migration 0077 |
  | `push_credential_scope` | same |
  | the `push_install` credential type | `agent_push/credential_scope.py`; the reverse rule in `remote/service.py`; the console label and its "not Domain Admin" warning in `frontend/src/modules/credentials/credentialTypes.ts` |
  | `agent.push.run`, `agent.push.scope.manage` | `app/core/rbac.py`, migration 0077, neither seeded to `technician` |
  | the batch and refusal logic | `agent_push/preflight.py` |
  | both kernel gate entries | `app/jobs/direct_dispatch.py`, `app/core/approved_actions.py`, held honest by `RESERVED_JOB_TYPES` |
  | the audit events | `push.authorization_created` / `_revoked`, `push.credential_scope_bound` / `_unbound`, `push.refused` |
  | the console screens | `frontend/src/modules/agent-push/`, at `/agent-push/authorizations` and `/agent-push/credential-scopes` |

  **IT REFUSES EVERYTHING AND DISPATCHES NOTHING**, which is §14's requirement
  and not an unfinished state. `POST /api/v1/agent-push/requests` runs the whole
  ladder and answers **403** (the caller lacks `credentials.checkout`), **422**
  (a target failed a bound — every failing bound named, per address) or **501**
  (every bound held and this platform cannot dispatch).
  `backend/tests/test_agent_push_dispatches_nothing.py` asserts from the AST
  that no source in the package names a job engine, a signer, a vault, a
  checkout, a socket or an SSH/WinRM client — so "B1 dispatches nothing" is a
  property of the package rather than a promise in a docstring.

  **§1.3's checklist is not all closed, and B1 did not need it to be.** Item 1
  (the amendment) is met. Item 3 (job-params credential handling) is open and
  is B2's to face; B1 neither decrypts nor signs anything, so it neither faces
  §6.4's problem nor pre-empts it. Item 2 (a dedicated credential that is not a
  domain admin) is a choice only the operator can make — B1 makes the type
  exist, refuses every other type on the push path and the push type everywhere
  else, and says on screen why it must not be Domain Admin. Items 4 and 5
  (published artefacts, packaging run by a human) are unchanged.

**FOUR PLACES THIS DOCUMENT IS NOW WRONG OR INCOMPLETE**, found while building
B1 and recorded rather than diverged from silently:

1. **§3 condition 1 no longer means what it says.** It requires "a `devices`
   row in this org" and argues it from promotion being the moment a human
   vouched for the machine. On the SAME DAY 2a was approved, the operator also
   ruled that a completed `net.sweep` writes a `devices` row for every host it
   reported, with nobody selecting anything (`docs/modules/asset-inventory/
   spec.md` §3, migration 0078) — so after that ruling the coffee machine §3
   warns about HAS a row, and a check written to §3's literal words would push
   to it. B1 implements the INTENT instead: `devices.provenance` must not be
   `sweep` (`preflight.VOUCHED_PROVENANCE`, and the test that would catch its
   removal). **§3's wording should be amended to say so.** The two rulings
   landed hours apart and neither document knew about the other.
2. **§3 says "the request row is recorded `refused`", and there is no request
   row in B1** — nor should there be. §5.3's `push_batch` exists to group JOBS
   for display, and B1 creates no jobs. The refusal is recorded as
   `push.refused` on the hash-chained audit log, one event per address, which
   is what §9 asks for and is stronger evidence than an application table. B2
   adds the batch row when there is something for it to group.
3. **§8 names two permission codes and no read code**, so B1's list routes are
   gated on `agent.push.scope.manage`. The consequence, stated rather than
   hidden: a holder of `agent.push.run` alone cannot browse the authorization
   list, and learns why a target was refused from the refusal itself, which
   names the failing bound and the permission that fixes it. Adding
   `agent.push.read` in the implementation would have been diverging from an
   approved spec quietly; if it proves wrong in use it is a decision to make
   deliberately.
4. **§4.3's `params_redactor`, §6.4's scrub-on-terminal and §5.3's circuit
   breaker are all B2**, because each of them is about a job, and there are no
   jobs. `agent.push.scope.manage`'s §8 description includes "clear a `suspect`
   credential"; that half of the code has nothing to gate yet.

One correction to §10.3 made while building it, recorded rather than silently
diverged: **step 4 reports and never gates.** §10.3 lists "review candidates,
promote" as step 4 and step 5 as what follows it, which reads as a sequence in
which promotion is a prerequisite. It cannot be one — a concurrent change makes
a sweep record what it finds as an asset directly, which turns that step into a
review *after the fact* and leaves the Discovered list empty in the ordinary
case. A journey that gated step 5 on "you have promoted these" would be wrong
the day that landed and would read as broken to an operator whose records
already exist. So step 4 names what to look at rather than what to do to it,
names *both* places a finding can land, and step 5's state does not depend on
it. Everything else in §10.3, including all three honesty rules, is built as
written.

**Where this file lives, and why not `docs/modules/asset-inventory/`.** The
request arrives from the inventory tab, but what it asks for is a reversal of a
decision recorded in *this* module: `docs/modules/agent-distribution/spec.md`
§1, "The decision that shapes everything: no push install", chosen by the
operator on 2026-08-18 over two named alternatives. `asset-inventory/spec.md`
§8 does not hold that decision, it *defers* to it — "Agent auto-deploy from a
candidate. Rejected in `docs/modules/agent-distribution/spec.md` §1 and not
reopened here." A spec that reopens a decision belongs beside the decision, or
the two documents disagree and a later reader has to guess which is current.
The console-journey half (§10) touches asset-inventory and IPAM; §10.5 names
exactly which lines in those specs would need amending, and under what
condition.

---

## 0. The request, split in two, because it is two requests

> "Or from the inventory tab itself, I need an option to scan subnets and add
> devices automatically, then from there I can deploy the agents"

Two things are being asked for, they have wildly different costs, and the
cheaper one is probably the one that prompted the message.

**Part A — one journey.** Everything up to "deploy the agents" already exists,
but it is spread across three nav groups and five screens: register a subnet
(IPAM), authorize the scan scope (IPAM), sweep (IPAM), review candidates
(Fleet → Discovered), promote (Fleet → Discovered), generate an install command
(Platform → Agent Packages). Six steps, three nav groups, no thread joining
them. That is very likely why the operator has not found this as a feature: it
is not a feature, it is six features that happen to compose. Part A is a guided
journey with one entry point on Devices & Inventory. It adds **no new
privilege, no job type, no credential, no socket**. §10.

**Part B — push install.** "Then from there I can deploy the agents", read
literally: select a discovered machine, and have the platform put an agent on
it. That means authenticating to a machine nobody has enrolled, using stored
administrative credentials, and executing code on it. §1 through §9.

**They are separable and should be separated.** Part A is a build cycle. Part B
is a decision first and two build cycles after it. Shipping Part A does not
commit anyone to Part B, and Part A is worth having whichever way Part B goes —
if Part B is never built, the journey ends at "here is the install command and
the machines that need it", which is a materially better answer than today's
"go find the Agent Packages page yourself".

---

## 1. What is actually being asked for, said plainly

Mechanism for mechanism, Part B is **lateral movement**: authenticate to a host
you do not manage using a privileged credential, write a binary to it, and make
it execute. There is no wording that makes this something else. Every red-team
report that ends badly contains this step, and every EDR product alerts on it.

That is not an argument that it must never exist — it is the single most useful
feature in an RMM, it is what SCCM client push, PDQ Deploy and NinjaOne's
network discovery all do, and an operator onboarding 250–300 endpoints by hand
has a real problem. It is an argument that the feature must be built so that
its worst case is small, stated, and bounded by something other than the
operator's care at the moment of the click.

### 1.1 The worst case, stated

**Today.** An attacker who fully owns the control plane can: read every
tenant's data, mint jobs *if* they also obtain the signing key from OpenBao,
and check out stored credentials. What they cannot do is reach a machine that
is not already running our agent. The platform's blast radius is *the managed
fleet*, and the boundary of that fleet is a set of machines somebody
deliberately enrolled.

**With Part B as literally requested** (console → pick an address → enter
credentials → install): the blast radius becomes *every machine the stored
credential can authenticate to*. For the domain-admin credential an operator
will reach for on day one, that is the entire estate — including machines this
platform has never managed, machines belonging to departments that never agreed
to be managed, and, on a flat network, machines belonging to somebody else. The
platform stops being a management tool with a fleet and becomes a domain-wide
authenticated code-execution primitive with a web UI.

**The intermediate case, which is the one that will actually happen.** Not a
compromised control plane: a mistaken click. An operator authorizes a /16
because typing a /16 is four characters, selects everything the sweep returned,
and pushes. The targets include a customer's kiosk, the finance director's
personal laptop on the guest VLAN, and a Siemens PLC that reboots when SMB
touches it. Nothing was compromised and the day is still ruined.

**And one that has no fix.** On first contact the platform **cannot
authenticate the target**. There is no host key, no certificate, no prior
relationship — that is what "unmanaged" means. Whatever answers 10.0.4.17 gets
presented with administrative credentials. On Windows that is an NTLM relay
waiting to happen: an attacker who answers a swept address collects, or relays,
the push account. This is a permanent property of every credentialed push
install ever built. It can be made expensive (§7.3). It cannot be removed.

### 1.2 The recommendation

**Build Part A now. Do not build Part B in this cycle.** Put amendment 2a
(§2.2) to the operator as a written decision. If they approve it, build Part B
in the narrowed form specified below, SSH-first (§7.4), in its own two cycles.

And a second recommendation the operator should weigh against Part B, because
it answers the same sentence at a fraction of the risk: **make third-party
distribution first-class.** A documented Intune/GPO package, an Ansible role,
and a token-from-key-vault recipe would let the operator "deploy the agents" to
everything the sweep just found, using mechanisms their estate already has,
that are already audited, and that require this platform to hold no
administrative credential at all. For a domain-joined Windows fleet — which is
most of Toeshee's 250–300 endpoints — GPO software installation is a better
answer than anything in §3–§9, and it is available today. Part B's honest
constituency is the machines GPO cannot reach: Linux servers, macOS, workgroup
and DMZ hosts. §7.4 has the uncomfortable consequence of that observation.

### 1.3 What would have to be true before Part B is built

Named, so this is a checklist and not a mood:

1. **The operator has approved amendment 2a in writing**, in `CLAUDE.md`,
   the way 1b was approved for MDM. Without it, §3's originator design is an
   invariant-2 argument made by an agent, which is not how invariants change.
2. **A dedicated push credential exists and is not a domain admin.** §6.2. If
   the answer to "what credential will you use" is "our Domain Admin account",
   Part B should not be built, because every bound in §5 and §6 is then
   protecting a credential whose theft ends the estate anyway.
   *Overturned for the Windows transport on 2026-09-28 by the operator (design
   `2026-09-28-install-link-and-windows-push-design.md` §0 item 4, Q7): the
   push account may be an existing Domain Admin. The console carries that as
   a standing warning, not a refusal, and design §2.3 "What remains" states
   the cost: a site server compromised during a push is a compromised domain.*
3. **Job-params credential handling is settled** — either the kernel gains
   vault indirection for job params, or this module ships scrub-on-terminal
   (§6.4). Shipping neither means a domain credential sits in plaintext in a
   `jobs` row for the retention period, and invariant 3 is not satisfied by
   "well, `backup.run` does it too".
4. **`agent_releases` has artefacts published for the target OS.** Today the
   catalogue is empty on the deployment (`spec.md` §0) and CI produces no
   downloadable binary. A push-install with nothing to push is not a feature.
5. **The Windows and macOS packaging has been run by a human at least once.**
   `spec.md` §3: the Windows service has never started, the macOS daemon has
   never started. A push that automates an install path nobody has performed
   manually automates an unknown.

---

## 2. The invariants, one at a time, and how each is satisfied

### 2.1 Invariant 2 — the server never opens an inbound connection to an endpoint

**This is the one that decides the shape of the whole feature.**

A control plane that SSHes or WinRMs into a discovered device violates
invariant 2 in letter and in spirit, and `spec.md` §1 already refused exactly
that. So the connection to the target must originate **inside the target's own
network segment**, from something the platform already trusts to be there.

Two candidates, and the codebase has already chosen between them once for a
closely related capability.

| Originator | Precedent | Verdict |
|---|---|---|
| Any enrolled agent with a current inventory observation inside the subnet | `ipam/sweep.py::select_sweeper` — this is exactly how `net.sweep` picks a sweeper | **Rejected.** See below. |
| A device carrying an enabled `site_server` row whose `segments` contain the target address | `site_server/registry.py`; `docs/01-architecture.md` describes site servers as the role for LAN-heavy work that must originate locally | **Recommended.** |

**Why not any on-segment agent, given that `net.sweep` does precisely that.**
Because of what the originator holds. `net.sweep` holds nothing: `netsweep.go`
opens a raw ICMP socket inside a verified job, closes it in a defer, and the
worst a compromised sweeper can steal is a list of addresses it could have
observed anyway. A push originator holds **a plaintext administrative
credential in process memory for the duration of the job**. Spreading that
across "whichever laptop happened to be online and had an observation in the
/24" is how a management tool becomes a credential-harvesting network. A
`site_server` row is an operator's deliberate, audited, revocable declaration
that a specific host is infrastructure. Concentrating credential exposure onto
declared infrastructure is the entire difference.

It also costs no kernel change: `docs/03-security-model.md` is explicit that a
site server **is an ordinary `Device` with an ordinary device-bound
certificate** and no certificate profile of its own. The push job is an
ordinary signed job to an ordinary device. Nothing about identity resolution at
the gateway changes.

**The segment with neither — a site server nor anything else.** This is a
first-class outcome and not an error to be swallowed. IPAM already has the
pattern: `state='no_agent'` is a recorded sweep outcome that the subnet page
renders, and `CandidatesPage.tsx`'s header explains why ("'nothing was found'
and 'nothing could look' render identically as a blank list and mean opposite
things"). Push install gets the same treatment:

- The refusal is `no_site_server`, recorded on the push request row, shown on
  screen with the remedy named: *"No site server serves 10.0.4.0/24. Promote a
  device on that segment to site server (requires `site.manage`), or install
  the agent from the install command."*
- **There is no fallback.** Not to another agent on the segment, not to a site
  server on a different segment, and above all not to the control plane. A
  fallback that silently widens who may hold a credential, or that quietly
  re-introduces a control-plane connection, is the exact failure this design
  exists to prevent. `provisioning/dispatch.py::_resolve_wake` already made
  this call for Wake-on-LAN — "no site server serves the target's segment =>
  refusal, before the run is created" — and the reasoning transfers verbatim.

**Invariant 2 is therefore unchanged in its own terms**: the server opens no
connection to any endpoint, and no agent listens. What is new is an agent
opening an outbound connection to a machine that is *not* an endpoint of this
platform. That is not covered by invariant 2's text either way, which is
exactly why it needs an amendment rather than an interpretation. §2.2.

### 2.2 Proposed amendment 2a — the text the operator is being asked to approve

Written to be pasted into `CLAUDE.md` beside 1b if approved, and structured the
same way, including the part that says what it does not claim.

> **2a. A machine that is not enrolled may be caused to run this platform's own
> agent installer, and nothing else, and only from inside its own network
> segment.** *(APPROVED by the operator 2026-08-26; the binding text now lives in `CLAUDE.md` as invariant 2a, and that copy governs — where the two differ, read `CLAUDE.md`. Invariants 1 and 2
> above are unchanged — not one word. This clause governs a path to machines
> that are not yet endpoints; it does not qualify either.)* All of the
> following are required:
>
> a. **The control plane never connects to the target.** Every packet that
>    reaches an unenrolled machine originates from a device the operator has
>    declared a site server for that machine's segment, acting inside a
>    signature-verified job. Where no such site server exists, the action is
>    refused and recorded; there is no fallback originator.
>
> b. **No operator-authored or platform-authored command travels.** The signed
>    parameters carry one target address, a transport, a release identified by
>    version and SHA-256, a single-use enrollment token and a credential. They
>    carry no command line, script, interpreter, argument string, package name
>    or artefact URL of anyone's choosing. The only thing that executes on the
>    target is the platform's own published agent artefact, placed by an
>    installer compiled into the agent.
>
> c. **The push mints no identity.** It delivers a single-use, time-bounded
>    enrollment token. A device identity is issued only by the ordinary
>    enrollment path, to a keypair the target generated itself and whose
>    private half never leaves it.
>
> d. **Three independent authorizations must all hold**: the target is a
>    promoted asset row in this organization; an active push authorization
>    covers its address; and the credential chosen is scoped to that address.
>    Any one absent is a refusal recorded before a signature is produced.
>
> e. **Same RBAC, same audit chain**, under permission codes of its own, and
>    never reachable by any automated, scheduled or AI-driven actor.
>
> f. **The destructive property is bounded, not removed.** A push may be
>    expressed only against explicitly named addresses a human selected — never
>    a group, a filter, a subnet or a saved search — and never more than the
>    configured batch ceiling in one act.
>
> **What 2a deliberately does not claim.** The platform cannot authenticate the
> target before presenting a credential to it; on first contact there is no
> host key, no certificate and no prior relationship. An attacker who controls
> the address receives an administrative credential, and on Windows may relay
> it. That is a permanent property of every credentialed push install, it is
> not fixed by any clause above, and the module's §7.3 does the accounting in
> full. It is bounded by (d), by credential dedication, and by the requirement
> that the credential be dedicated to this purpose and scoped to a prefix — not
> eliminated.

**The MDM carve-out 1b is not a precedent for this and is not cited as one.**
1b survives on a single load-bearing property — clause (a), *no
platform-authored executable payload* — which is what buys a "materially
smaller worst case than one that can [deliver code]". This path is nothing
*but* an executable payload. The only thing 2a borrows from 1b is its
*structure*: an explicit second clause, enumerated requirements, and an honest
statement of what is given up. Every substantive claim here stands on its own
evidence or does not stand.

### 2.3 Invariant 1 — every job is signed and verified

"Go install the agent on 10.0.4.17 using these credentials" is a job. It takes
the job path, unchanged: `JobEngine.create` → canonical bytes → signature by
the vault-held key → `signed_envelope` → mTLS WebSocket → the agent's
four-check runner (signature, target identity, freshness, non-replay) before
any capability runs. New job type `agent.push_install`, registered in
`app/jobs/registry.py` like every other. §4 specifies the params in detail; the
short version is that **what is signed is the whole instruction, and the
instruction contains no code**.

Two kernel gates must both refuse it — and `agent.push_probe` (§4.4) with
it — and a test must assert each allowlist
still partitions the live job-type registry — the pattern
`ipam/authorization.py` already documents for `net.sweep`:

- `app/jobs/direct_dispatch.py::MODULE_GOVERNED_REASONS` — so a holder of
  `jobs.dispatch` cannot mint one by hand at `POST /api/v1/jobs`, bypassing the
  authorization, the credential scope and the batch ceiling.
- `app/core/approved_actions.py::NON_APPROVABLE_REASONS` — so no approved
  action, no health remediation and no AI persona can ever name it. This is
  amendment 2a clause (e), enforced where it is checked rather than asserted in
  prose. It is also the same structural hole that was live for `net.sweep`
  ("packets went out with zero `ipam_scan_authorization` rows in the org"), and
  it is cheaper to close before the job type exists than after.

> **Amended by CLAUDE.md invariant 1c (2026-10-09):** an AI connected over the
> platform's MCP server acts as the person who connected it and may reach this
> act within that person's RBAC; the reservation above now governs automated
> and in-app-AI actors only. The approved-actions refusals
> (`NON_APPROVABLE_REASONS`) are unchanged.

### 2.4 Invariant 3 — credentials encrypted, decrypted only at point of use

§6 in full. Summary: the credential is stored in the existing vault
(`Credential` + `app/core/vault.py` envelope encryption), decrypted in the
dispatch service immediately before `JobEngine.create`, carried in the signed
params, held in the site server's process memory for the job's lifetime, zeroed
before the result is sent, never written to disk on the originator or the
target, never in a job log line, never in audit metadata, and removed from the
`jobs.params` row when the job reaches any terminal state.

The precedent that makes this arguable at all is `backup.run`, whose params
carry `repo_key` — a decrypted repository key — today. The precedent that
constrains it is `provisioning/enroll.py::_non_secret_post_config`, which
strips sealed credential fields out of `provision.enroll`'s params *entirely*
because that job has no need of them. **The rule those two cases together
establish: a credential rides in signed params only when the endpoint cannot do
the job without it, and never one field more than that.** A push cannot
authenticate to the target without the secret, so it qualifies; nothing else in
the params does, so nothing else is sealed material.

### 2.5 Invariants 4 and 6 — audited, RBAC-gated, and with its own permission

§8 (permissions) and §9 (audit). The headline: **`devices.manage` is not the
gate.** The authority to install code on a machine nobody has enrolled is not
the authority to edit an asset record, and collapsing them would hand every
technician who can rename a device the ability to run code on the estate. New
codes `agent.push.run` and `agent.push.scope.manage`, and `agent.push.run`
additionally requires `credentials.checkout` — the code that already governs
decrypting a stored secret — so push cannot become a side door around the
credential module's own gate.

### 2.6 Invariant 5 — tenancy

Every new table carries `org_id` with RLS. Note the one trap: `agent_releases`
is deliberately **global** and has no `org_id` (`spec.md` §4.2), so any join
through it must not be assumed tenant-filtered, and every push route enforces
RBAC explicitly rather than relying on RLS to catch a missing predicate. Cross-
org tests are pinned on the BYPASSRLS admin engine, per this project's standing
practice — through `org_session` RLS refuses first and a missing application
predicate passes unnoticed.

---

## 3. What may be a push target

Three conditions, all required, each independently revocable. They are separate
on purpose: each answers a different question, and collapsing any two loses the
question underneath.

**1. A promoted asset row.** The target is a `devices` row in this org with a
`primary_ip` equal to the target address. **Not a raw candidate.** A candidate
is an ICMP echo reply; `candidates.py` is explicit that "an echo reply is not
an asset — a sweep on a corporate LAN returns printers, phones, a colleague's
laptop on the wrong VLAN and the coffee machine". Promotion is the moment a
human asserted *this is a machine we own and manage*. Pushing to a candidate
would present administrative credentials to a coffee machine on the strength of
its having answered a ping.

This costs one click, and it is the click that carries the ownership claim.
§10.4 makes promote-then-push a two-step wizard so the workflow still *feels*
like one action while remaining two recorded decisions with two audit events.

**2. An active push authorization covering the address.** A new table,
`push_authorization`, modelled column-for-column on `ipam_scan_authorization`:
deny by default, prefix-scoped, `authorized_by` NOT NULL (no system-authorized
scope), never hard-deleted (revocation stamps `revoked_at`/`revoked_by`),
partial unique index on `revoked_at IS NULL`, every grant and revocation
audited, and — copying the one safeguard that matters most — **the intersection
with the target's subnet is taken first**, so an over-broad authorization can
never widen a push beyond the segment it is applied to.

**Deliberately NOT the same table as `ipam_scan_authorization`, and not implied
by one.** Authorizing an ICMP echo is not authorizing an administrative login.
That is the same argument `ipam/authorization.py` already makes one notch down
— "documenting is a filing action, authorizing packets against a range is a
different decision by a different person" — applied one notch up. An operator
who authorized a sweep of 10.0.0.0/8 last month has not consented to
credentialed logins across 16 million addresses, and a design that reads their
scan grant that way is putting words in their mouth.

**3. A credential scoped to the address.** §6.3.

**Refusal happens on the near side of the signing boundary.** For any target
failing any of the three, the request row is recorded `refused` with a named
reason and the call raises **before** `JobEngine.create` — so no job row exists,
no canonical payload is built and **no signature is ever produced**. This is
`sweep.py`'s structure and its rationale applies unchanged: revocation cannot
recall an already-signed in-flight job, so the only safe place to refuse is
before there is a signature.

---

## 4. The signed job

### 4.1 `agent.push_install` params — what they contain

```jsonc
{
  "target_address": "10.0.4.17",     // exactly one, an IP literal
  "expected_mac": "3c:a8:2a:…",      // optional; from the promoted asset
  "transport": "ssh",                 // fixed enum: ssh | winrm
  "os": "linux", "arch": "amd64",     // operator-declared, verified on target
  "release": {
    "version": "…", "sha256": "…",    // from `agent_releases`, server-read
    "url": "https://…"                 // presigned, short-lived, server-minted
  },
  "enroll_token": "…",                // single-use, ≤30 min, minted per push
  "credential": { "username": "…", "secret": "…" },  // decrypted at dispatch
  "host_key_fingerprint": "SHA256:…", // NEVER null — see §4.4
  "enroll_url": "https://itms.example.com"  // ITS_PUBLIC_CONSOLE_URL, a bare
                                     // https origin; server configuration
}
```

`enroll_url` (added 2026-09-29, additive) is where the installed agent enrolls:
the server's `ITS_PUBLIC_CONSOLE_URL`, never derived from the presigned
`release.url` (whose origin, on external object storage, is the storage
provider). An install is refused before the credential is decrypted when it is
not configured. It is a value, not a command: the agent validates it
(`layout.CheckEnrollOrigin`) before it fills the one slot in the fixed install
command.

Plus the kernel's own `issued_at` / `expires_at` / `nonce` / `device`.

### 4.2 What the params must NOT contain, and why each is named

- **No command line, script body, interpreter, shell string or argument list.**
  Not one field, not optional, not "advanced". The only code that runs on the
  target is the published artefact and the installer compiled into the pushing
  agent. This is amendment 2a clause (b), and it is the difference between "a
  feature that installs our agent" and "a remote code execution API with a
  default payload". `deploy.script.run` exists for running code — on machines
  that are *enrolled*, under a pinned `scripts` row, with a `script_run` record.
  Nothing about this path is a substitute for it, and the two must never be
  reachable from the same screen.
- **No operator-supplied artefact URL.** The URL is presigned by the server
  from the `agent_releases` row the server selected. An operator-supplied URL
  is an operator-supplied payload with extra steps.
- **No more than one target.** Not a CIDR, not a list, not a hostname that
  resolves to several addresses. A batch of N is N jobs (§5.2).
- **No group id, no filter, no saved search.** Amendment 2a clause (f), and the
  same rule invariant 1b(f) reached for destructive MDM actions. A filter is
  evaluated at dispatch time against a set the operator did not see.
- **No `install_args`, no `post_config`, no free-form dict.**
  `approved_actions.py`'s `SECRET_PARAM_KEY_MARKERS` comment names the failure
  mode precisely: a free string field is where a credential gets smuggled in
  ("`app.deploy`'s `install_args` could carry `/PASSWORD=...`"). This params
  model is `extra="forbid"` with no free-form member at any depth.

### 4.3 Registration properties

- `params_redactor` — **mandatory**, not optional. `GET /jobs/{id}` is gated on
  `jobs.read`, a far weaker permission than `agent.push.run`; the redactor
  removes `credential` and `enroll_token` and `release.url` from every API read
  path. `registry.py` documents this extension point for exactly this case.
- `params_refresher` — **none, and deliberately.** A refresher exists so a
  retry can re-mint time-bounded material. Push installs are **not retried
  automatically** (§5.3), so there is nothing to refresh.
- `cancellable` — **yes**. A push in flight must be interruptible; `job.cancel`
  takes the ordinary signed path.
- **TTL: 300 seconds**, not the kernel's 3600. `net.sweep` uses 600 because
  "the TTL is the exposure window" for a revoked *authorization*. Here the TTL
  is the exposure window for a decrypted *credential* sitting in a signed
  envelope and a database row, which is worth more, so the window is shorter. A
  push that has not started inside five minutes should be re-authorized by a
  human, not resurrected by a timer.

### 4.4 `agent.push_probe` — the credential-free first contact

`host_key_fingerprint` in §4.1 is never null, which means something must learn
it before the push exists. That something is a **second job type carrying no
credential at all**.

`agent.push_probe` takes `target_address` and `transport` and nothing else. It
opens the transport, completes the key exchange, records the host key
fingerprint and the server banner, and closes. **It never authenticates, so it
presents no credential**, and its params model has no field that could carry
one. The operator confirms the returned fingerprint in the console; it is
pinned on the asset row; only then may a push be dispatched.

Two consequences, stated rather than left to be discovered:

- **A push to a host with no pinned key is refused, not prompted.** The probe
  is a separate, explicit act. A design that fetched the fingerprint inside the
  push job would have to hold the credential across the operator's decision, or
  present it before the decision — both of which defeat the point.
- **The OS/arch check cannot move into the probe.** Reading `uname` requires a
  login, and the probe has no credential. So §7.2's verification stays inside
  the push job: after authentication, before any transfer.

`agent.push_probe` is subject to everything §4.2 and §4.3 require of
`agent.push_install` — one target only, no command of any kind, both kernel
gates (§2.3), the same permissions (§8), the same audit chain — with one
difference: it needs no redactor, because it signs nothing worth redacting.
Its TTL may be the kernel default; there is no credential to bound.

---

## 5. Scale, blast radius and partial failure

### 5.1 What bounds a single mistaken click

Four independent bounds, and the worst case is their product:

| Bound | Set by | Widened by |
|---|---|---|
| Batch ceiling | `PUSH_BATCH_MAX` (recommend 25) | code change |
| Push authorization prefixes | `push_authorization` rows | `agent.push.scope.manage`, audited |
| Credential scope prefixes | `push_credential_scope` rows | `agent.push.scope.manage`, audited |
| Site-server segments | `site_server.segments` | `site.manage`, audited |

**Worst case with all four in place:** 25 machines, all inside one authorized
prefix, all on a segment an operator declared, receive the platform's own
signed agent artefact using a credential dedicated to push and scoped to that
prefix. That is the intended effect misapplied to the wrong 25 machines —
noisy, embarrassing, and recoverable by uninstalling.

**Worst case with any one bound removed:** every machine the credential can
authenticate to receives code execution. There is no graceful degradation
between those two numbers, which is why none of the four is optional and why
each is widened only by an admin-tier, audited act.

### 5.2 Groups, filters and batches

**Never a group, never a filter, never a subnet.** §4.2, amendment 2a (f).

**A bounded batch of explicitly named addresses: yes, recommended.** One
request carries up to `PUSH_BATCH_MAX` addresses a human selected from a list
they were shown, and expands server-side into that many independent jobs.

The alternative — strictly one target per request, one modal at a time — is
what invariant 1b(f) reaches for with remote erase, and it was seriously
considered. It is rejected here for a reason worth stating: **a rule nobody can
follow gets worked around.** An operator onboarding 200 machines through 200
modals will instead find the credential, and push from a laptop with psexec,
outside the platform, unaudited. A ceiling of 25 with a confirmation screen
that names every address is a bound that will actually be respected. The
ceiling is the safety property; the batch is the ergonomics.

Validation is **all-or-nothing, execution is per-target**: if any address in
the batch fails any of §3's three conditions, the whole request is refused with
the offending addresses named, before any job is created. An operator must not
discover mid-batch that four of their twenty-five were outside the
authorization.

**Since 2026-09-30, a subnet may build a list; it is never the thing confirmed.**
CLAUDE.md 2a(e) and 2a(f) carry dated notes, approved with
`docs/superpowers/specs/2026-09-30-gpo-msi-and-subnet-push-design.md`: an
authorized subnet expands to the vouched-for machines it holds, a person
confirms that named list (bound by a SHA-256 digest), and a push run installs
at most 1,024 of them, re-checking every bound above before each one. A run
sends one machine first and at most 50 in flight only after a sign-in has got
through; three sign-in failures since it last started or resumed stop it as
`stopped_credential` until a person resumes it (design §1.4a). A push that is
not a run keeps the ceiling of 25 described here. §5.3's per-credential
circuit breaker (`PUSH_AUTH_FAILURES_MAX`) is still unbuilt.

### 5.3 Partial failure

- **N targets = N jobs**, each with its own `jobs` row, terminal state, result
  and audit event. A `push_batch` row groups them for display only; it holds no
  aggregate state that could hide a failure. A batch is never "succeeded".
- **No automatic retry.** Three reasons and each is sufficient: the credential
  would have to be re-decrypted and re-signed (§6.4 removes it after the
  terminal state); a failed authentication repeated across a batch is how an
  AD account gets locked out; and a push that failed for an unknown reason
  against a machine the platform cannot inspect is exactly the situation where
  a human should look before it happens again.
- **Auth-failure circuit breaker.** After `PUSH_AUTH_FAILURES_MAX` (recommend
  3) consecutive authentication failures on one credential, that credential is
  marked `suspect` and further pushes with it are refused until an admin
  clears it. Repeated auth failure against unmanaged machines is either the
  wrong credential — moments away from locking out an account across the
  estate — or somebody probing with a stolen one. Both want a stop.
- **Account lockout is called out on screen** before the click, because it is
  the most likely real-world harm and it is not obvious: pushing a wrong
  domain credential at 25 machines can lock the account for everyone.

---

## 6. The credential

### 6.1 Where it is decrypted, what holds it, for how long

| Stage | Holder | Lifetime |
|---|---|---|
| At rest | `credentials.ciphertext` + wrapped DEK (vault envelope encryption) | indefinite |
| Dispatch | control-plane process memory, inside the request that mints the job | one call — decrypt immediately before `JobEngine.create` |
| In transit | signed job params, over the mTLS WebSocket | ≤ 300 s (TTL) |
| At rest, transiently | the `jobs.params` row | until terminal state, then scrubbed (§6.4) |
| Execution | the site server's agent process memory | the job's duration; zeroed before the result is sent |
| Target | **never persisted** — presented to the authentication exchange and discarded | the session |

Never in: a log line, an audit metadata dict, an API response (the redactor,
§4.3), a job log, the artefact, the target's disk, argv on either machine. The
`packaging/README.md` rule that install scripts **refuse `--token`** applies
with equal force to the credential: nothing goes in argv, on either side.

### 6.2 The credential is dedicated to push, and it is not a domain admin

A new credential `type='push_install'`. A credential of this type may not be
used by remote-control or provisioning, and their types may not be used here.
Enforced server-side and tested. Reuse means one theft costs every subsystem at
once and makes rotation impossible to reason about.

**And the console must say, at the point of creation, that this credential
should be a dedicated account with the minimum rights needed to install a
service — not Domain Admin.** This is the one lesson from SCCM's client-push
account that is worth importing wholesale: that account is a documented lateral-
movement enabler precisely because everybody made it a domain admin and pointed
it at every subnet. The platform cannot enforce what rights an account holds in
somebody else's directory. It can refuse to pretend the choice does not matter,
and §1.3 item 2 makes "not a domain admin" a precondition for building this at
all.

*Amended 2026-09-28 (D10 overturned; design §0 item 4, §2.5).* A second
dedicated type, `windows_domain` ("Windows domain account"), carries the
account a Windows push presents over Kerberos. It is dedicated exactly as
`push_install` is — never usable by remote control or provisioning — and
scoped by prefix the same way (§6.3). The operator chose an existing Domain
Admin for it (Q7), so "not Domain Admin" is no longer a precondition for the
Windows transport: it is a **standing warning** on the Credentials page and in
the push panel, saying what the choice costs rather than pretending to check it.

### 6.3 Scoping: by prefix, not by device group

`credentials.device_group_id` scopes a credential to *devices*. A push target
is not yet a managed device and may not be in any group, so that column cannot
carry this. A new `push_credential_scope` binds `(credential_id, prefix)`, and
a push is refused unless the target address falls inside a prefix bound to the
chosen credential.

This is the direct answer to *how does one credential not get sprayed at the
whole estate*: **the credential itself carries where it may be presented.**
Binding a new prefix is `agent.push.scope.manage` — admin tier, audited, and
visible on the credential's own page as a list of prefixes with their address
counts, following `ScanAuthorizationsPage.tsx`'s rule that every row states its
reach in addresses rather than in mask lengths.

### 6.4 The uncomfortable part: `jobs.params` is a database column

Signing requires canonical bytes, and this platform's canonical bytes are the
stored params. So at dispatch the credential plaintext **is** written to the
application database in the `jobs` row. There is precedent — `backup.run` ships
`repo_key` this way today — and there is a difference that matters: `repo_key`
unlocks one device's own backup repository, while a push credential unlocks
machines.

`approved_actions.py` already names the correct fix, in the codebase, in a
comment: *"The real fix is vault indirection — a template holding a reference
that is resolved at point of use — which this platform does not have for job
params yet."* That is a kernel change and it is out of this module's scope.
Until it exists, this module's requirement is:

> **The credential plaintext must not survive the job's terminal state.** On
> `succeeded`, `failed`, `refused`, `expired` or `cancelled`, the `credential`
> and `enroll_token` fields in `jobs.params` are overwritten. The plaintext's
> life in the database is bounded by the 300-second TTL, not by the job
> retention period.

Falsifier: `SELECT params FROM jobs WHERE type = 'agent.push_install' AND
status IN (terminal states)` returns any credential material.

**This is a residual, not a solution**, and it is why §1.3 item 3 puts
"job-params credential handling is settled" on the list of things that must be
true first. A reviewer who thinks scrub-on-terminal is insufficient is not
wrong; they are arguing for the kernel change, and that argument should be had
before this is built rather than after.

---

## 7. Identity, and the target we cannot authenticate

### 7.1 The push mints no device identity

This is the question *"how is the installed agent's identity established so a
push-install is not a way to mint a device identity out of thin air"*, and the
answer is that the push has no identity-minting power whatsoever.

The push delivers a **single-use enrollment token**. The installed agent then
performs the ordinary enrollment from `docs/05-agent-spec.md`: generates a
keypair on the target (private half never leaves it), builds a CSR, submits it
with the token, and the internal CA issues a certificate bound to a device
identity and `org_id`. **The pushing site server never sees the private key,
never sees the certificate, and cannot enrol anything on the target's behalf.**

So push install confers exactly the power `agent.release.read` already confers
through `POST /agent/releases/{id}/install-command`: the ability to mint an
enrollment token. It adds a delivery mechanism, not an identity mechanism.

Tightenings over the console-minted token, because this one is delivered by a
machine rather than typed by a human:
- `max_uses = 1` and expiry ≤ 30 minutes (the console default is hours).
- The token row records the push job id, so `bind_pushed_device` — mirroring
  `provisioning/enroll.py::bind_provisioned_device` — correlates the enrolment
  back to the push, the promoted asset and the operator who authorized it, and
  audits `push.enrolled`. An enrolment that cannot be correlated is an
  enrolment nobody can attribute.

**Falsifier:** any code path in this module that writes an `agents` row, writes
a `devices` row for the target, or calls the CA, without the target having
enrolled itself.

### 7.2 The target's OS is not known, and must not be guessed

A discovered candidate carries an address, sometimes a MAC, an OUI-derived
vendor and sometimes reverse DNS. It carries **no OS**. So `os`/`arch` in the
params are operator-declared, and the pushing agent **verifies them on the
target after authenticating and before transferring anything** (over SSH:
`uname -s -m`). It cannot be checked any earlier: reading `uname` needs a
login, and the probe of §4.4 deliberately has no credential. A mismatch is
a clean failure with a named reason, never a best-effort install of the wrong
binary. An OUI is a manufacturer, not an operating system, and inferring one
from the other is exactly the kind of guess `candidates.py` refuses for
hostname matching.

### 7.3 The target cannot be authenticated on first contact

Stated in §1.1 and accounted for here in full, because it is the guarantee this
feature does not have.

**SSH.** No known host key exists on a first push. That is trust-on-first-use,
and TOFU with an administrative credential is a real exposure. Mitigation: the
credential-free `agent.push_probe` of §4.4 fetches the fingerprint first, the
console shows it, and the operator confirms it **before any credential is
presented**. Onboarding a host is therefore two dispatches, not one, and the
spec accepts that cost. The fingerprint is pinned on the asset row; a later
push to a host whose key has changed is refused rather than warned about, and a
push to a host with no pinned key is refused rather than prompted.

**SMB/WinRM.** An attacker answering the address can capture or relay the
credential to a third host. Available hardening: require Kerberos with a target
SPN rather than NTLM; require SMB signing / channel binding; refuse a target
whose observed MAC differs from the one recorded at promotion. Each raises the
cost; none closes it. **The honest conclusion is that the Windows transport
cannot be made safe against a hostile LAN — only expensive.**

**What follows from that** is §7.4, and it is uncomfortable.

### 7.4 SSH first, Windows only under a separate decision — and why that is awkward

The safe transport is SSH: key- or password-based with a fingerprint the
operator confirms, no relay class, a BSD-licensed client
(`golang.org/x/crypto/ssh`) that suits the open-core posture, and targets
(Linux servers, macOS) that GPO cannot reach anyway. **Recommendation: build
SSH only in the first Part B cycle.**

The awkward part, said plainly rather than buried: **the safe half is the half
the operator needs least.** Toeshee's 250–300 endpoints are mostly Windows, and
mostly domain-joined — which is the population GPO/Intune already serves well
(§1.2) and the population whose push transport carries the relay exposure. So
SSH-first delivers a genuinely safe feature to a minority of the fleet, and
defers the majority to a mechanism that already exists and works.

That is not a reason to build the Windows transport anyway. It is a reason for
the operator to ask whether Part B earns its cost at all once GPO is doing the
Windows fleet — which is Open decision D2, and the most consequential one in
this document.

> **2026-09-28: the Windows transport now exists, under Kerberos only** (D10
> overturned by the operator). WinRM on 5986 with channel binding, or on 5985
> with Kerberos message encryption, never unencrypted; NTLM, Basic and CredSSP
> are not compiled into the agent. §7.3's "only expensive" conclusion is
> answered for Kerberos — not for NTLM, which is why NTLM is refused — in
> `docs/superpowers/specs/2026-09-28-install-link-and-windows-push-design.md`
> §2.3, and the one-time setup is `deploy/WINDOWS-PUSH.md`.

---

## 8. Permissions

| Code | Tier | What it grants |
|---|---|---|
| `agent.push.run` | **admin**, not seeded to `technician` | Dispatch a push install against named, already-authorized targets. Additionally requires `credentials.checkout`. |
| `agent.push.scope.manage` | admin | Grant/revoke a `push_authorization` prefix; bind/unbind a `push_credential_scope`; clear a `suspect` credential. |

**Its own code, not a reuse.** Not `devices.manage` (editing an asset record is
not executing code on a machine), not `deploy.script.run` (that runs code on
*enrolled* machines from a pinned script row), not `agent.release.read` (that
mints a token for a human to carry), not `provisioning.run` (that targets a
machine being imaged under a run record that authorized it).

**Admin tier, deliberately, and this may be the wrong call for a busy
helpdesk.** The argument for admin-only: this is code execution against
machines that are not under management, which is a strictly larger act than
anything a technician does today, and `health.action.admin`'s description sets
the precedent — *"configuring what alarms and executing code on endpoints are
different acts and different blast radii"*. The argument against: onboarding is
routine technician work, and an admin bottleneck on onboarding will be routed
around. Open decision D8; the recommendation is to start admin-only, because
loosening a permission later is a decision and tightening one is an incident.

**`agent.push.run` additionally requires `credentials.checkout`.** Without this,
push would be a way to *use* a stored secret while holding only the permission
to see its name — a side door around the credential module's own gate. The
pattern is `ai.act.confirm`'s: *"the confirming user must ALSO hold the
permission the owning module's own dispatch route requires"*, and
`provisioning`'s `wake_on_lan` additionally requiring `site.wake`.

---

## 9. What must be audited

Every one of these, to the hash-chained log, tenant-scoped, and **never
carrying the secret**:

- `push.authorization_created` / `push.authorization_revoked` — actor, prefix,
  address count, note.
- `push.credential_scope_bound` / `push.credential_scope_unbound` — actor,
  credential id, prefix.
- `push.dispatched` — actor, target address, promoted asset id, credential id,
  site-server device id, release version + sha256, enrollment-token id, job id.
- `push.refused` — actor, target address, and *which* of §3's three conditions
  failed. A refusal that does not say which is a refusal nobody can act on.
- `push.result` — job id, outcome, failure reason.
- `push.enrolled` — correlating the target's enrolment back to the push (§7.1).
- `push.credential_suspended` / `push.credential_cleared` — the circuit breaker.
- Plus the existing `enrollment.token_created` and the kernel's own job-dispatch
  event, unchanged.

**The pairing is the point**, exactly as `spec.md` §5 argues for
`install_command_generated` + `enrollment.token_created`. The question this
feature creates is: *which human caused an administrative login to that
machine, with which credential, from which host, and what got installed on it?*
An auditor must be able to answer that from the chain alone, and every field
above exists because one clause of that sentence needs it.

---

## 10. Part A — the one journey, and where its entry point belongs

This half is buildable now, requires no amendment, and is the part most likely
to have prompted the operator's message.

### 10.1 What is wrong today

Nothing is missing. Everything is scattered. The six steps live at `/ipam`
(subnets), `/ipam/scan-authorizations`, `/ipam` again (sweep), `/discovered`
(candidates, promote) and `/agent-packages` (install command), across three nav
groups, with nothing linking them and no screen that shows how far along a
given subnet is. An operator who has never read the IPAM spec has no way to
learn that authorizing a scan scope is a prerequisite for a sweep — they will
find the sweep button, get nothing, and conclude the feature is broken.

### 10.2 The entry point

**Devices & Inventory (`/devices`) gains a primary action, "Add devices",** with
two paths:

- *"I have the machine in front of me"* → the existing install-command flow at
  `/agent-packages`, unchanged.
- *"Find machines on a network"* → the journey at **`/devices/onboard`**.

**In the Fleet nav group, not IPAM.** The person doing this is doing fleet
work, and `nav.ts` already made this exact call for Discovered: *"Beside the
fleet list, not under Platform: this is inventory work, and the operator doing
it is the operator looking at devices."* IPAM's pages remain the authority for
the admin-tier acts, and the journey **deep-links into them** with a return
path — it does not duplicate their controls.

### 10.3 The journey

One page, five (or six) steps, each showing its own state and the remedy when
it is not satisfied:

1. **Is the network registered?** → the org's `ipam_subnet` rows, with "Register
   a subnet" linking to IPAM (`ipam.manage`).
2. **Is scanning authorized here?** → live status from
   `ipam_scan_authorization`, showing the covering prefix and its address count.
   Not satisfied → link to `/ipam/scan-authorizations` (`ipam.scope.manage`).
   **This step is why the journey exists**: it is the prerequisite nobody
   discovers on their own.
3. **Has it been swept?** → last `ipam_sweep_run`: when, by which device, or
   `no_agent` rendered as its own state with its own remedy, never as an empty
   list. Action: sweep now (`ipam.sweep.run`).
4. **What was found?** → the candidate list, inline, with the evidence-quality
   labels `CandidatesPage.tsx` already defines ("ping only" / "ARP" / "reported
   by an agent"). Bulk select, promote.
5. **Get an agent onto them** → for each promoted asset with no agent, the
   install command (existing flow), and the honest sentence about what the
   platform will and will not do next.
6. *(Only if Part B is approved)* → **Push install**, §10.4.

**Three honesty rules, carried from the pages this journey wraps.** They are
requirements, not polish:

- A step the caller lacks permission for says *"an administrator must do this"*
  and names the permission. It is never hidden, because a hidden prerequisite
  is indistinguishable from a broken feature.
- `no_agent` and "nothing found" are visually distinct and worded distinctly.
- No step renders a count it cannot substantiate. The Devices page's refusal to
  render columns nothing backs is the standard: *"a screen that looks finished
  and lies is worse than one that is visibly incomplete."*

### 10.4 Where push would attach

Step 6, on promoted assets only, and **never a control on the candidate list**.
The promote → push wizard shows promote and push as two numbered steps with two
confirmations, because they are two decisions (§3 condition 1). The
pre-flight confirmation before the push is dispatched must show, in this order:

1. **Every target address, listed** — no "and 22 more". The operator must be
   able to read the whole list.
2. The credential, by name, with its scope prefixes and their address counts.
3. The site server that will perform the push, and the segment it serves.
4. The release version and SHA-256 that will be installed.
5. The enrollment token's expiry and that it is single-use.
6. The host-key fingerprints awaiting confirmation (§7.3).
7. One plain sentence, not a tooltip: **"This presents administrative
   credentials to machines that are not managed by this platform and cannot be
   verified by it."**
8. The account-lockout warning (§5.3).

This is `ScanAuthorizationsPage.tsx`'s discipline applied to a bigger act: that
page states a scope's reach in *addresses* because nobody converts mask lengths
in their head. This screen states a push's reach in *machines and credential*,
for the same reason.

### 10.5 What this changes in other specs — and only if Part B is approved

Part A alone changes no other spec; it is a new route and a new page over
existing APIs, plus one primary action on `/devices`.

Part B, if approved, requires three deliberate amendments, listed so they are
made rather than discovered:

1. `docs/modules/agent-distribution/spec.md` **§1** — the "no push install"
   decision, and **§7** — "What this deliberately does not do". Both must be
   rewritten to record that the decision was revisited, on what date, what changed,
   and what did not (invariant 2 in its own terms is still unbroken: the server
   still opens no connection to an endpoint).
2. `docs/modules/agent-distribution/spec.md` **§6 falsifier 7** — *"Something in
   this module opens an outbound connection to an endpoint"*, currently a
   grep-level guard over the module for socket/WinRM/SSH clients. That guard
   must be **narrowed, not deleted**: the control-plane packages still may not
   contain one, and the new agent capability is the single exempted location.
3. `docs/modules/asset-inventory/spec.md` **§8** — *"Agent auto-deploy from a
   candidate. Rejected in agent-distribution §1 and not reopened here"* — which
   stays substantially true even then, because §3 refuses to push from a
   candidate. The amendment is to say *from a promoted asset*, not *from a
   candidate*.

---

## 11. The agent side

A new capability package, `agent/internal/capabilities/pushinstall/`,
registering `agent.push_install`. It touches the transport, enrollment and
signed-job runner not at all — the capability model in
`docs/05-agent-spec.md` is followed as written.

The same package registers `agent.push_probe` (§4.4), which shares the
transport-dialling code and reaches no further than the key exchange.

**Behaviour inside one verified `agent.push_install` job**, in order: verify
the target address is the one in the signed params and nothing else; open the
transport; confirm the host key against `host_key_fingerprint` and abort on any
mismatch; authenticate; verify `os`/`arch` (§7.2); download the
artefact from the presigned URL and **verify its SHA-256 against the signed
params before transferring a byte to the target**; transfer; place the
enrollment token as a `0600` root-owned file (never argv, `packaging/README.md`);
register the service; report; zero the credential; close everything.

**How the service is registered over SSH (2026-09-29).** The pushing agent
creates the root-only staging directory `/opt/toeshee`, places the verified
artefact at `/opt/toeshee/agent.pkg` and the token at
`/opt/toeshee/enroll.token` (0600), and runs one fixed command against the
placed binary: `/opt/toeshee/agent.pkg install --token-file
/opt/toeshee/enroll.token --enroll-url '<enroll_url>'`. That binary's install
mode (`agent/internal/install/pushunix.go`, the "installer compiled into the
agent" of clause (b)) does what `agent/packaging/linux/install.sh` and
`agent/packaging/darwin/install.sh` do, to the same layout, and deletes both
staged files. The push account must log in as **root**; nothing on this path
elevates.

**The endpoint never decides what it may touch.** The target address comes only
from signed params, exactly as `site.wake`'s broadcast address is computed by
the control plane and signed in (`docs/01-architecture.md`: *"the agent
broadcasts to that address and nothing else, so the endpoint never decides what
it may touch"*). No discovery, no fallback address, no retry against a
neighbour.

**Idle budget — a design target with falsifiers, per `CLAUDE.md`, and described
as a target until the tests exist.** Measured through `internal/budget`'s
harness in `pushinstall/budget_test.go`, from ordinary `Test` functions that
*compare* against a threshold — never `Benchmark` functions, because
`go test -bench` exits 0 on a regression and this project has measured that.
The falsifiers:

1. An idle agent with this capability compiled in holds zero goroutines and
   allocates zero bytes for it. (`netsweep/budget_test.go` falsifier 1 is the
   model.)
2. No socket, file handle or transport client exists between jobs.
3. The credential is zeroed before the job result is sent, and appears in no
   log line at any level.
4. The capability refuses a target address that is not in the signed params.
5. Peak allocation is bounded by the artefact size, not by the segment size or
   the batch size — one job, one target, one artefact, streamed.
6. An artefact whose SHA-256 does not match the signed digest is never
   transferred.

Falsifiers 3, 4 and 6 are security assertions rather than budget ones, and are
tested alongside because they measure the same code paths.

---

## 12. Falsifiers for the whole feature

What observation would show a guarantee broken. Each should be a test.

**Part A**
1. A journey step renders a count or a status it cannot substantiate.
2. `no_agent` renders identically to "nothing found".
3. A step the caller lacks permission for is hidden rather than explained.
4. The journey mutates anything IPAM owns without going through IPAM's routes
   and permissions.

**Part B — invariants**
5. Any control-plane package opens a connection to a target address. (The
   narrowed successor to `spec.md` §6 falsifier 7.)
6. A push is dispatched to a device with no enabled `site_server` row covering
   the target address.
7. A signature is produced for a target failing any of §3's three conditions.
   (Assert the AST shape `sweep.py` asserts: the authorization check precedes
   anything that can produce a signature.)
8. Either job type — `agent.push_install` or `agent.push_probe` — is
   dispatchable via `POST /api/v1/jobs`, or nameable by an approved action, an
   AI persona or a schedule.
9. The params model accepts any command, script, interpreter, argument, package
   name or artefact URL — at any depth, including through a free-form dict.
10. The params model accepts more than one target, a CIDR, a group id or a
    filter.
11. A batch above `PUSH_BATCH_MAX` is accepted, or a partially-invalid batch
    creates any job at all.

**Part B — credentials**
12. `GET /jobs/{id}` returns the credential, the token, or the presigned URL.
13. Terminal `agent.push_install` rows retain credential material in
    `jobs.params`.
14. A credential appears in an audit metadata dict, a job log, an agent log, or
    argv on either machine.
15. A `push_install` credential is usable by remote-control or provisioning, or
    vice versa.
16. A push succeeds against an address outside the chosen credential's bound
    prefixes.
17. The circuit breaker does not trip after `PUSH_AUTH_FAILURES_MAX`
    consecutive auth failures.

**Part B — identity**
18. Any path in the module writes an `agents` row, writes a `devices` row for
    the target, or calls the CA, without the target enrolling itself.
19. An enrollment token minted for a push has `max_uses > 1` or an expiry
    beyond 30 minutes.
20. An enrolment resulting from a push cannot be correlated back to the push.
21. A push proceeds against a host whose pinned key has changed, or against a
    host with no pinned key.
22. `agent.push_probe`'s params model accepts a credential field, or its
    capability authenticates.

**Part B — tenancy and RBAC**
23. Any push route answers without a permission check, or answers for another
    org's asset, credential, authorization or site server. Pinned on the
    BYPASSRLS admin engine as well as through the API.
24. `agent.push.run` is granted by `devices.manage`, `deploy.script.run`, or by
    a role that does not also hold `credentials.checkout`.

---

## 13. Explicitly out of scope

- **Any push transport other than SSH in the first Part B cycle.** §7.4.
- **Agentless management.** A push installs an agent; it does not become a way
  to run things on machines that never get one. If a target's install fails, it
  stays unmanaged.
- **Credential discovery, spraying, or "try these credentials".** One
  credential, chosen by name, scoped to the prefix. Nothing iterates.
- **Uninstall/repair over push.** A machine with an agent is managed over the
  signed-job path; that is what the path is for.
- **Port or service scanning to determine the transport.** `net.sweep` is ICMP
  and ARP, and `asset-inventory/spec.md` §8 keeps it that way. The operator
  declares the OS (§7.2); the platform verifies it after authenticating, and
  does not probe to guess it beforehand.
- **Auto-promote.** Open decision D1 — flagged, not decided here.
- **Scheduled or recurring push.** Amendment 2a (e): no automated actor.

---

## 14. Sizing

- **Part A** — one implementation plan. New route, new page, read aggregations
  over existing APIs, one primary action on `/devices`. No migration, no job
  type, no permission.
- **Part B** — **two** plans minimum, and they must not be merged:
  - *B1, control plane:* `push_authorization`, `push_credential_scope`, the
    `push_install` credential type, the two permission codes, the batch and
    refusal logic, the two kernel gate entries, the audit events, the console
    authorization and credential-scope screens. Ends with a feature that
    refuses everything, correctly, and dispatches nothing.
  - *B2, the jobs and the agent:* both job types (§4.4's probe and the push),
    the redactor, dispatch,
    scrub-on-terminal, the Go capability, the budget tests, the pre-flight
    screen, the enrolment correlation.

  B1 is buildable and testable with no agent code and no push ever leaving the
  building, which is the right place to find out whether the refusals hold.

---

## 15. Open decisions

Every judgement call in this document that a real dialogue would have asked
about. Each has a recommendation, a rejected alternative, and is
**overturnable** by the operator.

**D1. "Add devices automatically" vs the documented refusal to auto-promote.**
*Flagged, not decided — this one is explicitly the operator's.*
The request says devices should be added automatically. The platform refuses
auto-promotion, deliberately, in three places: `asset-inventory/spec.md` §3
("discovery produces candidates; an operator promotes them"), `candidates.py`
("an echo reply is not an asset — a sweep on a corporate LAN returns printers,
phones, a colleague's laptop on the wrong VLAN and the coffee machine"), and
`CandidatesPage.tsx` ("bulk promote is allowed; bulk auto-promote is not, and
the difference is that the selection came from a human looking at the rows").
Falsifier §6.1 of asset-inventory tests it: three addresses observed, nothing
else run, the device table asserted empty.
*The case for changing it:* onboarding a known estate means confirming several
hundred rows the operator already knows about, and an inventory that requires a
click per machine will simply not be filled in. A middle path exists and is
worth considering on its own: auto-promote only rows whose MAC matches an
existing asset record or an OUI on an operator-maintained allowlist, leaving
everything else as a candidate — which preserves "a human decided this class of
thing is ours" while removing the per-row click.
*My recommendation, weakly held:* keep the refusal, add the middle path if
onboarding volume proves it necessary. The reasoning behind the refusal is
sound and the coffee machine is real. But this is the operator's estate and
their call, and the existing reasoning is recorded above so they can rule on it
with it in front of them.

**D2. Build Part B at all?**
*Recommendation:* not this cycle, and possibly not at all for the Windows
fleet. Build Part A; put amendment 2a to the operator; and weigh Part B against
making GPO/Intune/Ansible distribution first-class, which answers the same
sentence at a fraction of the risk (§1.2, §7.4).
*Rejected:* building it as literally asked (console → pick an address → enter
credentials → install), which is the design §1.1 describes as turning the
platform into a domain-wide code-execution primitive.
*Overturnable* — and note that the operator already declined push install once,
on 2026-08-18, with a site-server-mediated variant explicitly on the table. It
is entirely possible they are re-asking without realising it is the same
question; it is also possible the second ask means the first answer no longer
fits. Only they can say which.

**D3. Originator: site server only.**
*Recommendation:* only a device with an enabled `site_server` row covering the
segment. No fallback; `no_site_server` is a first-class refusal.
*Rejected:* `select_sweeper`-style auto-selection over any agent with an
observation in the subnet — rejected because the originator holds a plaintext
administrative credential and `net.sweep`'s originator holds nothing (§2.1).
*Overturnable*, but this is the load-bearing bound in the design; loosening it
should come with a replacement bound, not on its own.

**D4. Credential in signed job params (Option A) vs control-plane-side
authentication over an agent relay (Option B).**
*Recommendation:* Option A. Precedent exists (`backup.run.repo_key`,
`remote.session.open.relay_token`), the credential's exposure is bounded to
declared infrastructure by D3, and the Go SSH client is BSD-licensed, which the
open-core posture wants.
*Rejected:* Option B — the control plane authenticates over a TCP relay the
agent holds open, so the plaintext never leaves the control plane. It is
genuinely **better on credential exposure** and worse on everything else: it
needs an SMB/SSH client stack in the control plane (Python SSH options carry
copyleft-adjacent licences), and it turns the relay — which today terminates
only at the agent's own host — into a general LAN pivot primitive, which is a
larger and more reusable capability than a bounded install job.
*Overturnable, and here is what should flip it:* if the operator's push
credential ends up being a domain admin after all, Option B's argument becomes
much stronger, because keeping that plaintext off endpoints entirely is then
worth the pivot surface.

**D5. Job-params vault indirection as a prerequisite.**
*Recommendation:* not a hard blocker; ship scrub-on-terminal (§6.4) and record
the residual. Vault indirection for job params is the durable fix, it is a
kernel change, and it would benefit `backup.run` equally.
*Rejected:* blocking Part B on the kernel change — rejected because it would
hold a module hostage to a refactor nobody has scoped, and because the
plaintext window with scrub-on-terminal is 300 seconds rather than the
retention period.
*Overturnable*, and a reviewer who insists on the kernel change first has a
legitimate position (§6.4).

**D6. Batch ceiling of 25 named addresses.**
*Recommendation:* a bounded batch, ceiling 25, all-or-nothing validation, never
a group or filter.
*Rejected:* strictly one target per request, which is what invariant 1b(f)
reaches for with remote erase — rejected because an operator facing 200 modals
will push from a laptop with psexec instead, outside the platform and
unaudited (§5.2).
*Overturnable*, and the number itself is a reasoned default, not a measured
one.

**D7. Target must be a promoted asset, not a raw candidate.**
*Recommendation:* promoted asset. Promotion is where a human claimed ownership.
*Rejected:* pushing directly from the candidate list — which is what the
operator's sentence literally asks for — rejected because it would present
administrative credentials to anything that answered a ping (§3).
*Overturnable*; §10.4's two-step wizard is the mitigation that keeps the
workflow short without collapsing the two decisions into one.

**D8. `agent.push.run` is admin tier.**
*Recommendation:* admin only initially, additionally requiring
`credentials.checkout`. Loosening a permission later is a decision; tightening
one is an incident.
*Rejected:* technician tier, on the grounds that onboarding is routine
technician work — a real argument, and the reason this is a decision rather
than a fact (§8).
*Overturnable.*

**D9. `push_authorization` is a separate table from `ipam_scan_authorization`.**
*Recommendation:* separate. Authorizing an ICMP echo is not authorizing an
administrative login, and an operator who authorized a /8 sweep has not
consented to credentialed logins across 16 million addresses.
*Rejected:* reusing the scan authorization, which is cheaper to build and would
have put words in the operator's mouth (§3).
*Overturnable*, but this one should be hard to overturn.

**D10. SSH first; Windows transport is a separate decision.**
*Recommendation (2026-08): SSH only in B2; Windows/SMB/WinRM requires its own
decision with §7.3's relay accounting in front of the operator.*
**OVERTURNED 2026-09-28 by the operator.** The Windows transport is built, over
**WinRM with Kerberos only** — NTLM, Basic and CredSSP refused with no fallback.
The relay accounting §7.3 deferred Windows for is done in
`docs/superpowers/specs/2026-09-28-install-link-and-windows-push-design.md` §2.3:
with Kerberos the password is used only against the domain controller, and what
reaches the target is a ticket bound to `HTTP/<fqdn>` that only the real computer
account can open and that cannot be relayed to another service. That closes the
capture-and-relay exposure for Kerberos, which is why NTLM is refused rather than
allowed as a convenience. The residual — a compromised site server holds the
password in memory for the push's duration — is stated in design §2.3 "What
remains" and bounded by credential dedication and prefix scoping, not removed.
*This overturn is itself overturnable, but only back to SSH-only; the Kerberos-only
constraint is not a convenience to relax.*

**D11. Should amendment 2a's "no automated or AI-driven actor" clause live in
`CLAUDE.md` or stay module-local?**
*Recommendation:* in `CLAUDE.md`, as clause (e). Invariant 1b(f) put the
equivalent MDM rule there, the two kernel gates (`NON_APPROVABLE_REASONS`,
`MODULE_GOVERNED_REASONS`) are kernel-level and not module-level, and a rule
about what machines may never do belongs where the machines' authors read.
*Rejected:* keeping it in this spec only, which is smaller but leaves the
strongest constraint in the least-read place.
*Overturnable.*
