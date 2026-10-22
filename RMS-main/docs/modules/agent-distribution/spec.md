# Agent distribution — spec

**Status:** **built**, 2026-08-18, to the plan at
`docs/superpowers/plans/2026-08-18-agent-distribution.md`. Approved 2026-08-18
(operator chose "installer + token, you distribute" over any push-install
option). The runbook is `deploy/README.md`, "Publishing the agent to the
console".

**Six of the seven falsifiers in §6 are covered by tests; §6.3 is covered in
half.** Falsifiers 1, 2, 4, 5 and 7 are in
`backend/tests/test_agent_release_api.py` and
`backend/tests/test_agent_distribution_opens_no_connection.py`; 6 is in
`frontend/src/modules/agent-releases/AgentReleasesPage.test.tsx`. **§6.3's
"byte-identical for two orgs" half is not run.** Its other half is — the object
key is derived from `(version, os, arch)` with no org component
(`test_objectstore_agent_release.py`), and the catalogue is provably shared
(`test_the_catalogue_is_shared_across_orgs_by_design`) — but the download
itself needs an object store, and this suite has none. It is asserted from the
key derivation, not observed.

**What is built is not the same as what is proven.** Nothing here has been
exercised against a real deployment: no artefact has been published to a
running install, and no endpoint has been enrolled from a console-generated
command. The tests cover this module's logic and its permission checks against
a real Postgres; they do not cover MinIO's presigned GET as this deployment
configures it. §3's Windows and macOS rows are unchanged for the same reason —
**publishing an artefact does not run it.**

---

## 0. Why the deployed console shows an empty catalogue (2026-08-25)

Filed by the operator as "no agent packages". **It is not a defect in this
module**, and the investigation is recorded here so the next person does not
repeat it. Every route, the permission seed, the nav entry (`shell/nav.ts`,
gated `agent.release.read`) and the page itself are present and wired; the
catalogue is empty because **nothing has ever been published to it, and three
separate decisions mean nothing ever publishes itself**:

1. **The platform never builds or fetches the agent** — §1. It stores bytes an
   operator uploads. By design, and the design is invariant 2.
2. **CI produces no downloadable binary.** `.github/workflows/ci.yml`'s
   `agent` job cross-compiles all six targets to `/dev/null` (a compile check)
   and builds a `.deb` only to test-install it inside the job. Nothing calls
   `actions/upload-artifact`, and there is no release workflow — the file
   triggers on `push: [main]` and `pull_request` only. §8.1 records CI
   publishing as an open question; this is that question still open, and it is
   why the first step of publishing is "install Go".
3. **The console has no publish surface** — §4.5 specifies read plus
   install-command and nothing else. Publishing is the two API calls in
   `deploy/README.md`.

**And since migration 0070, the runbook as written cannot be followed by the
account it addresses.** `deploy/README.md`'s "Publishing the agent to the
console" says `agent.release.manage` is "seeded to `org-admin` only" and tells
the operator to use "the same admin bearer token used throughout Step 11".
0070 moved that code to `super-admin` for the reason §4.2 gives, and
`super-admin` is not grantable from any console surface — it is a direct
`user_roles` insert. So the deployment's everyday `org-admin` account gets
`403 requires agent.release.manage` at the first call, from a runbook that
does not mention it.
`tests/test_agent_release_api.py::test_an_org_admin_cannot_publish_and_a_super_admin_can`
pins the behaviour; the runbook is what is stale. **Correcting that paragraph
is outstanding** — it lives in `deploy/`, which this cycle did not own.

What this cycle changed: the console's empty state now names the cause and
carries the three publishing steps, including the digest comparison §4.4 rests
on, and tells a reader without `agent.release.manage` that the refusal is
by design rather than letting them discover it as a 403 after a build and an
upload. Nothing about publishing, checksums or download URLs moved.

---

## 0.1 Managed distribution is now a first-class path (2026-08-26)

The operator ruled on 2026-08-26 that **managed distribution (GPO/Intune) is
built as a first-class path, and push install is built too.** The first is
`docs/modules/agent-distribution/spec-managed-distribution.md`, built the same
day; the second is `spec-push-install.md` §1–§9 and is another cycle's.

**Nothing in §1 below is reversed by that, and nothing in §2 is weakened.** A
managed deployment is the estate's own directory *pulling* on its own schedule
— the control plane still opens no connection to any endpoint, and
`tests/test_agent_distribution_opens_no_connection.py` still passes over this
module unchanged. §2's rule that an artefact is token-free and byte-identical
for every tenant is not merely preserved but *load-bearing*: a GPO distribution
point is read by every machine account in the domain, which is the most
distributable artefact this platform will ever touch.

What did have to be answered is the one §4.4 explicitly deferred — *"For a
fleet rollout the operator should use the token-free artefact URL plus their own
secret distribution"* — namely **what that fleet credential actually is**. It is
a bounded multi-use enrollment token: `max_uses` and expiry required and capped
on a route of its own, where `POST /enrollment/tokens` defaults `max_uses` to
null and null means unlimited. The blast radius, what bounds it, and the two
stronger bounds that need their own spec rather than a build cycle are
spec-managed-distribution.md §3.

§4.5's console page gains one action (**Managed deployment**) and §4.4's
one-machine command is unchanged.

---

**What this is.** How an operator gets the agent onto a machine that has no agent
yet, from the console, without the control plane ever connecting to that machine.

**What this is not.** A remote-install button. That was considered and
deliberately rejected — see §1.

---

## 1. The decision that shapes everything: no push install

The obvious feature request is "select a machine, enter credentials, install the
agent". It was raised, and it was declined because it breaks a security
invariant rather than merely stretching it:

> **Invariant 2.** Agents connect outbound only, over mutual TLS. **The server
> never opens an inbound connection to an endpoint.** No agent listening port.

A credentialed push means the control plane connects *to* the endpoint over
SMB/WinRM/SSH holding administrative credentials. That is precisely the property
invariant 2 exists to deny. It is not a formality: today, an attacker who owns
the control plane still has to wait for agents to dial out and still cannot forge
a job, because job signing happens in OpenBao. Add a credentialed push path and
they gain an immediate, authenticated way into every machine those credentials
reach — which for a domain admin credential is the whole estate.

Two alternatives were offered and not taken: a site-server-mediated push (same
violation, smaller blast radius) and a control-plane push under an explicit
amendment written the way invariant 1b was for MDM. The operator chose neither.

**So invariant 2 is unchanged by this module, and nothing in this spec may
introduce an outbound connection from the platform to an endpoint.** That is the
acceptance criterion a reviewer should check first.

What the operator gets instead: the artefact, and a command to run. Distribution
is theirs — GPO, Intune, PDQ, Ansible, a login script, or a technician at the
desk. This is how most agents are actually deployed in practice, and it costs the
platform no new privilege.

### 1.1 The decision was revisited on 2026-08-26, and what did NOT change

*(Required amendment, `spec-push-install.md` §10.5 item 1. Recorded here rather
than left for a reader to reconcile: a document that still reads "declined" while
two authorization tables ship is a document a later reader has to guess about.)*

**What changed.** The operator was asked the question a second time and, shown
the accounting in `spec-push-install.md` — including §7.3, the part that cannot
be fixed — **approved amendment 2a on 2026-08-26**. The binding text lives in
`CLAUDE.md` beside 1b. A machine that is not enrolled may now be caused to run
this platform's own agent installer, **and nothing else, and only from inside
its own network segment**.

**What did NOT change, and it is the sentence above.** Invariant 2 is unbroken
in its own terms: *the server still opens no connection to any endpoint, and no
agent listens.* What 2a permits is an ENROLLED agent — specifically, a device
the operator declared a site server for that segment — opening an outbound
connection to a machine that is not an endpoint of this platform, inside a
signature-verified job. The control plane connects to nothing. §1's third
paragraph above described a CONTROL-PLANE push and remains an accurate
description of a design that was rejected then and is still rejected now.

**Which of the two alternatives §1 offered was taken.** The first: a
site-server-mediated push, under an explicit amendment written the way 1b was
for MDM. Both halves — the mediation and the written amendment — were required,
and neither substitutes for the other.

**What is built.** Cycle B1 only (`spec-push-install.md` §14): the
`push_authorization` and `push_credential_scope` tables, the dedicated
`push_install` credential type, `agent.push.run` / `agent.push.scope.manage`,
the batch and refusal logic, both kernel gate entries, the audit events and the
two console screens. **It refuses everything and dispatches nothing.** There is
no `agent.push_install` job type, no agent capability and no code under
`agent/` — those are cycle B2, and the cycles "must not be merged".

**The second recommendation still stands and is not superseded.**
`spec-push-install.md` §1.2 argues that first-class GPO/Intune/Ansible
distribution answers the same operator sentence at a fraction of the risk, and
`spec-managed-distribution.md` is that work. For a domain-joined Windows fleet
it remains the better answer, and §7.4's uncomfortable consequence is
unchanged: the safe transport is the one the fleet needs least.

---

## 2. The constraint that kills the obvious implementation

The natural design is "download an installer with the enrollment token baked
in". **`agent/packaging/README.md` already forbids it, in as many words:**

> **A distributable artifact** — A token inside a `.deb` is copied to file
> servers, baked into golden images and cached by apt. One image build and the
> whole fleet shares one token.

That document names three places a token must never reach — argv, a service
definition, and a distributable artefact — and the shipped install scripts
enforce the first by **refusing `--token`**. A pre-seeded installer would violate
the third, and would do it in the most durable way available: an artefact that
gets copied and cached.

Therefore:

- **Artefacts are token-free and byte-identical for every tenant.** One
  `its-agent-windows-amd64.exe` serves every org. This also means they can be
  cached and mirrored freely, which is a distribution advantage, not just a
  security one.
- **The token travels separately**, per install, and reaches the endpoint by the
  mechanism `packaging/README.md` already specifies: one file,
  `/var/lib/its-agent/enroll-token`, mode `0600`, root-owned, which the agent
  reads once and **deletes** on successful enrolment.

The console's job is to hand the operator both halves and a command that keeps
them apart.

**Amended 2026-09-28 (operator's decision, install-link spec §0 item 1).**
"Artefacts are token-free and byte-identical for every tenant" still holds for
the BYTES. The install link puts a link code, and the console's address, in
the downloaded file's NAME
(`ITMS-Agent-Setup_<host>[@<port>]_<code>.exe`) and the code in the page's
one-line `install.sh`, where it stays in the Downloads folder of every machine
installed that way. A link code is not an enrollment token — it enrols only
while the link is on, only from a listed network, under an hourly limit, and
only into *Waiting for review* — and the exposure is bounded by spec §1.6 and
recorded in full in `agent/packaging/README.md`, "The install link: a code in
a file name".

**Amended 2026-09-30 (operator's decision, GPO MSI spec Q3).** One artefact is
no longer byte-identical per tenant: the Group Policy MSI
(`docs/superpowers/specs/2026-09-30-gpo-msi-and-subnet-push-design.md` Part 2)
wraps the published, release-signature-verified agent together with
`msi-enroll.json`, which holds the console's address and the organisation's
link code. It is built per request for an administrator holding
`enrollment.link.manage`, never stored, and audited without the code. The code
on a SYSVOL share is readable by every domain user and computer; it only
places a machine in *Waiting for review*, and **Regenerate** revokes it. The
residuals are recorded in `agent/packaging/README.md`, "The Group Policy MSI".

---

## 3. What exists today, and what does not

| Piece | State |
|---|---|
| `agent/packaging/build.sh` | Cross-compiles **six static binaries** (`linux/amd64,arm64`, `windows/amd64,arm64`, `darwin/amd64,arm64`) into `agent/dist/`. Works. |
| `agent/packaging/linux/` | `build-deb.sh`, systemd unit, `install.sh`, `uninstall.sh`. **The tested path.** |
| `agent/packaging/windows/` | `install.ps1`, `its-agent-enroll.ps1`, `uninstall.ps1`. **No MSI.** **Never executed by anyone** — reviewed line by line on 2026-08-26 and eleven defects fixed by reading (`agent/packaging/README.md`, "Found by reading"), which is not the same thing as having run it. |
| `agent/packaging/darwin/` | launchd plist, `install.sh`, `uninstall.sh`. **Never executed by anyone**, same review, same caveat. |
| A release or artefact endpoint | **Did not exist when this was written; built by this cycle.** `/api/v1/agent/releases` and the Platform → Agent Packages page now serve it, and `EnrollmentPage.tsx`'s header — which used to document the gap — points at them. The rest of this table is unchanged, and still describes the packaging as it stands. |
| Enrollment tokens | Exist, with an API and a console page. |

**The marker comes off by evidence, and there is now a procedure for producing
it.** `docs/modules/agent-distribution/first-run-verification.md` is the exact
sequence to execute each path once on a real machine — every command, what to
observe at each step, what each observation would falsify, and the four places
(this table, `agent/packaging/README.md`, the console's per-row marker and
`deploy/README.md`) that must be updated together when it is retired. Reading
the scripts carefully found eleven real defects on 2026-08-26 and could not
have found a twelfth class: whether the SCM accepts the dispatcher at all,
whether `Set-Acl` behaves, whether `launchctl bootstrap` accepts the plist, and
whether TCC lets a root daemon read system inventory. Those need a machine, and
until one has been used the honest word is still "never executed".

**There is no MSI and this spec does not add one.** *(Still true after
2026-08-26. `spec-managed-distribution.md` §5.3 records what that costs in
practice: GPO's Software Installation policy takes an MSI and nothing else, so
managed deployment there is a computer startup script — a different supported
mechanism with different diagnostics. Two things WERE added to
`agent/packaging/windows/`: `its-agent-enroll.ps1`, so installing and enrolling
can be separate steps at all, and a fix to `install.ps1`, which started an
unenrolled agent into a restart loop.)* An MSI needs WiX or
equivalent in CI, and Windows code signing needs the operator's certificate.
Both are real work with an external dependency, and neither is required to get an
agent onto a Windows machine: `install.ps1` plus a signed binary is deployable by
Intune, PDQ, SCCM and a login script. If an MSI is later wanted, it is its own
cycle — noted in §8.

---

## 4. Design

### 4.1 Where artefacts live

Object storage, under a **platform-owned prefix** (`agent-releases/<version>/`),
not a per-org one. The artefacts are identical for every tenant, so per-org
copies would multiply storage for no isolation benefit — and the isolation that
matters here is that a tenant cannot *write* a release, which is an RBAC
property, not a prefix property.

This reuses the presign machinery the deploy module already has
(`app/modules/deploy/api.py`'s `presign_package_artifact` and
`upload_package_artifact`), rather than inventing a second artefact path. Read
that code before writing this; the size cap, the `Content-Length` handling and
the refusal on a missing artefact are all patterns to copy rather than re-derive.

### 4.2 The `agent_releases` table

One row per (version, os, arch):

| Column | Notes |
|---|---|
| `id` | uuid7 |
| `version` | the git short SHA the binary was built from, or a semver tag |
| `os`, `arch` | `linux\|windows\|darwin`, `amd64\|arm64` |
| `storage_ref` | object-storage key |
| `size_bytes` | shown in the console, and checked on upload |
| `sha256` | **hex digest, computed server-side on upload, never supplied by the client** |
| `signed` | bool — whether this artefact carries a platform code signature |
| `published_at`, `published_by` | audit trail |
| `notes` | optional |

No `org_id`. This table is deliberately **global**, and that is the one place
this module departs from "every table is tenant-scoped" — with a consequence:
RLS cannot be the tenant guard here, so **every route must enforce RBAC
explicitly**, and a reviewer should treat a missing permission check on these
routes as the same class of defect a missing `org_id` filter is elsewhere.

`sha256` is computed by the server from the bytes it received. A client-supplied
digest verifies nothing — it is the same party supplying the bytes.

**And the write code follows from "global", which took until migration 0070 to
say out loud.** If the table has no `org_id`, then `agent.release.manage` is not
scoped by tenant either: whoever holds it publishes, replaces and withdraws the
artefact *every* endpoint on the deployment downloads and runs. Migration 0060
seeded it to `org-admin`, so on a multi-tenant deployment any customer's own
administrator could replace every other customer's agent binary — code
execution across the tenant boundary, on every managed machine. 0070 moves the
code to `super-admin`, the cross-org platform-operator role no org-scoped
console surface can grant, clone or assign, and adds it to
`app/core/rbac.PLATFORM_OPERATOR_CODES` so an `org-admin` holding
`roles.manage` cannot tick it back on.

`agent.release.read` deliberately **stays** with `org-admin`. Listing the
catalogue and minting an install command is what installing an agent *is*
(§4.4), it writes nothing, and taking it away would stop every tenant onboarding
a machine without the operator while closing nothing.

### 4.3 Routes

| Route | Permission | Purpose |
|---|---|---|
| `GET /api/v1/agent/releases` | `agent.release.read` | list, newest first |
| `POST /api/v1/agent/releases` | `agent.release.manage` | register a version (metadata) |
| `POST /api/v1/agent/releases/{id}/artifact` | `agent.release.manage` | upload the bytes; server computes `sha256` |
| `POST /api/v1/agent/releases/{id}/install-command` | `agent.release.read` **and** the existing enrollment-token create permission | mint a token + a presigned download URL, return a ready command |
| `DELETE /api/v1/agent/releases/{id}` | `agent.release.manage` | unpublish |

The permissions in this table are unchanged by migration 0070; what changed is
who holds `agent.release.manage` — see §4.2's last paragraphs.

The last one is the interesting route and §4.4 is about it.

### 4.4 The install command

`POST …/install-command` does two things that are each already audited
individually — mints an enrollment token, and presigns an object read — and
returns a command that **installs, registers and starts a real service** while
carrying the token **on stdin or in an inherited environment variable, never in
argv**.

**It runs the reviewed installer; it does not re-implement it.** The command
downloads the binary, verifies it, and then runs the exact `install.sh` /
`install.ps1` that `agent/packaging/` ships — the scripts that write the
hardened systemd unit or break the Windows data-directory ACL down to
SYSTEM+Administrators, configure the restart actions, and start the service only
once it has a token to spend. Those scripts travel **with** the command,
base64-encoded, because the machine being installed onto holds neither this
repository nor a credential to fetch them and the platform opens no connection
to it (invariant 2). The backend carries byte-identical copies under
`backend/app/modules/agent_distribution/installers/` (the container image does
not include the agent tree), and `backend/tests/test_installer_scripts.py` pins
each to its `agent/packaging/` original so the console can never hand out a
script `packaging_test.go` did not review.

Linux (schematically — the base64 blobs are elided):

```sh
set -e; d=$(mktemp -d)
curl -fsSL '<presigned-url>' -o "$d/its-agent"
printf '%s  %s' '<sha256>' "$d/its-agent" | sha256sum -c -
printf %s '<base64 install.sh>'        | base64 -d > "$d/install.sh"
printf %s '<base64 its-agent.service>' | base64 -d > "$d/its-agent.service"
chmod +x "$d/install.sh"
printf '%s' '<token>' | sudo bash "$d/install.sh" \
  --token-stdin --url '<control-plane-url>' --binary "$d/its-agent"
```

Windows spawns `install.ps1` the same way, with the token in
`ITS_AGENT_ENROLL_TOKEN` (which the child inherits and the script reads) and
`-Binary` / `-Url` as the ordinary, non-secret arguments the installer and
first-run-verification.md A3.2 both expect to be visible.

Two defects the field hit are gone with the old shape. The Windows checksum is
computed with `[Security.Cryptography.SHA256]` over the file's bytes rather than
`Get-FileHash` — the cmdlet the operator saw as `not recognized`, absent on the
Windows PowerShell 5.1 a fleet still runs. And the command now passes the
control-plane URL (`--url` / `-Url`), derived from the request the operator is
authenticated on; the previous command passed none, so even a machine that
already had `install.ps1` would have staged a service holding a token with no
address to spend it at.

The `sha256sum -c` step is **not decoration**. The download is over the
operator's own `:443`, which on an internal deployment is very often an
internally-issued certificate; a checksum the operator can compare against what
the console displays is what makes the artefact verifiable independently of
whatever the TLS chain happens to be that day.

**What a human must still confirm.** This command is generated and its structure
is tested (`test_agent_release_api.py`, `test_install_command_shape.py`), and its
Linux half is exercised end to end against `install.sh --destdir` staging. The
Windows and macOS halves cannot be run by this CI at all: that the SCM accepts
the dispatcher, that `Set-Acl` takes, that `powershell -File` runs the decoded
script under Bypass, and that a stop is clean are exactly the observations
`docs/modules/agent-distribution/first-run-verification.md` Parts A and C exist
to make on a real machine. Do not read "the tests pass" as "the path works end
to end on Windows".

**The token is still visible in the operator's shell history**, because it is a
literal in the command they paste. This is a real residual and the spec states it
rather than hiding it: single-use, short-expiry tokens are the mitigation, and
the console must default to `max_uses: 1` and an expiry measured in hours. A
token that is spent by the time the shell history matters is a weak credential
by design.

For a fleet rollout the operator should use the **token-free** artefact URL plus
their own secret distribution (an Intune script pulling the token from a key
vault, an Ansible `no_log` task). The generated command is for one machine at a
time, and the console should say so.

### 4.5 Console page

**Platform → Agent packages**, and the `enrollment` module's page gains a link to
it (that is where an operator already goes to think about enrolment).

Per release row: version, OS, arch, size, `sha256` (copyable), signed flag,
published date. Primary action: **Generate install command** → a modal with the
OS-appropriate command, a copy button, the token's expiry, and an explicit
"this token is single-use" line.

Platform-honesty requirements, in the spirit of the Devices page's refusal to
render columns it cannot fill:

- Windows and macOS rows carry a visible **"never executed on this platform"**
  marker until someone has run them, because `agent/packaging/README.md` §"What
  is untested" says the Windows service has never started, the macOS daemon has
  never started, and `systemctl` was never run against a live system.
- An unsigned artefact says `unsigned`, not nothing.

---

## 5. What must be audited

Every one of these, to the hash-chained log, tenant-scoped where a tenant exists:

- `agent.release_published` / `agent.release_unpublished` (actor, version, sha256)
- `agent.install_command_generated` (actor, release, **token id**, device none)
- the token mint itself, which already writes `enrollment.token_created`

The pairing matters: `install_command_generated` plus `enrollment.token_created`
lets an auditor answer "who was handed the ability to enrol a device, and for
which artefact" — which is the question this feature creates.

---

## 6. Falsifiers

What observation would show this broken? Each of these should be a test.

1. **A release route answers without a permission check.** The table has no
   `org_id`, so RLS will not save a forgotten `require_permission`. Assert every
   route refuses an unauthenticated and an under-privileged caller.
2. **`sha256` is accepted from the request body.** Plant a wrong digest in the
   upload request and assert the stored value is the one the server computed.
3. **A token appears in an artefact.** Assert the uploaded bytes are byte-identical
   for two different orgs' downloads of the same release, and that no artefact
   path is per-org.
4. **The install command puts the token in argv.** Assert the generated string
   passes the token via a pipe or stdin redirect and never as an option value —
   the same rule the install scripts already enforce by refusing `--token`.
5. **`max_uses` defaults above 1, or expiry is unbounded.** Assert the default.
6. **A Windows or macOS row renders without the untested marker.** It is a
   platform-honesty claim, so it needs a test like the Devices page's
   fabricated-column test.
7. **Something in a CONTROL-PLANE package opens an outbound connection to an
   endpoint.** The invariant-2 check. A grep-level guard for socket/WinRM/SSH
   clients is crude but catches the regression that matters
   (`backend/tests/test_agent_distribution_opens_no_connection.py`).

   **NARROWED ON 2026-08-26, NOT DELETED** (`spec-push-install.md` §10.5 item
   2). Amendment 2a permits an ENROLLED site server to dial a machine that is
   not an endpoint, inside a signed job. That agent capability is the single
   exempted location and it does not exist yet — it is cycle B2, under
   `agent/internal/capabilities/pushinstall/`. Every control-plane package is
   still forbidden one, including the new `backend/app/modules/agent_push/`,
   and `backend/tests/test_agent_push_dispatches_nothing.py` asserts it there
   from the AST: no socket, no SSH, no WinRM, no HTTP client, and — while cycle
   B1 stands — no signer, no job engine and no vault either.

   The narrowing is a widening of what the platform may do and must be read as
   one. What keeps it bounded is that the exemption is a NAMED PACKAGE rather
   than a permission: a second control-plane package that opened a socket would
   still be a defect, and this falsifier would still catch it.

---

## 7. What this deliberately does not do

- **No push install FROM THE CONTROL PLANE, ever.** §1, and §1.1 for what the
  2026-08-26 ruling did and did not change. A push originates from a device the
  operator declared a site server for the target's segment, inside a
  signature-verified job, and there is no fallback originator — where no such
  site server exists the action is refused and recorded. The control plane
  connects to nothing, which is the property invariant 2 asserts and 2a leaves
  untouched.
- **No push install AT ALL in this build.** Cycle B1 is what shipped
  (`spec-push-install.md` §14): the authorizations, the credential scoping, the
  permissions, the refusals, the audit events and the console. A fully
  authorized request is answered 501. The job types, the signing and the agent
  capability are cycle B2.
- **No `.pkg`, no signed Windows binary, no signed MSI.** §3. *(Amended
  2026-09-30: the Group Policy MSI exists — §2's amendment — and it is
  unsigned, like the binary it wraps. Managed distribution is what makes an
  unsigned file reach three hundred machines instead of one, and once the MSI
  is on a distribution point the share's ACL is the only control left — see
  `spec-managed-distribution.md` §4 and the console's SHA-256, shown for the
  file just downloaded.)*
- **No auto-update.** The agent does not update itself, and nothing here changes
  that. A release page makes it *easier* to see that endpoints are behind, which
  is worth having before an updater exists rather than after. *(2026-09-23:
  self-update was approved as its own cycle, `spec-self-update.md`. It builds on
  this module's `agent_releases` rows and adds a `channel` column.)*
- **No unauthenticated download endpoint, except the install link's page.**
  Every artefact read is a presigned, time-bounded grant minted for an
  authenticated operator — except the install link's page, its Windows
  download and its `install.sh` (`/i/<code>`), which are public by the
  operator's decision of 2026-09-28 (install-link spec §0 item 2). They answer
  only while the link is on, only a request from a listed network, and
  otherwise exactly the 404 of a path that does not exist; the download is
  still a 15-minute presigned grant, minted by the server for that request.
  An open binary download is a supply-chain surface with no upside here, which
  is why the exception is this narrow.

---

## 8. Open questions for the next cycle

1. **Who uploads artefacts?** This spec assumes an operator uploads what
   `build.sh` produced. CI publishing directly is better (fewer hands on the
   bytes) but needs a credential for CI to hold, which is its own decision.
2. **MSI and code signing.** Wanted eventually; needs a toolchain in CI and the
   operator's signing certificate. **Promoted to the top of this list on
   2026-08-26**, for the reason in §7 above: on the managed path nothing
   verifies the artefact at install time, so an Authenticode signature is the
   only check that could exist between the distribution point and three hundred
   endpoints. It is a procurement question, not an engineering one.

   **Written up as a decision the operator can act on:**
   `docs/modules/agent-distribution/authenticode-signing.md` (2026-08-26) —
   the three ways to obtain a key and what each costs, why there is no longer a
   `.pfx` to put in a CI secret (CA/Browser Forum hardware requirement, June
   2023, which the current `agent/packaging/README.md` signing table predates),
   where the signing step goes and why it must run **before** the digest is
   taken, what changes for the SHA-256 comparison, the engineering tail that
   lands in this repository, and — at length — **what it does not fix**. The
   short version of that last part: *Windows does not verify Authenticode
   before running an executable.* Signing creates the possibility of a check;
   a WDAC or AppLocker publisher rule creates the check. Signing alone makes a
   substitution detectable and publisher-bound at the staging step, which is
   real and is not "the endpoint verifies before installing".

   It is also the missing half of **OQ-3 / self-update** for Windows, provided
   the signing authorisation lives somewhere the control plane cannot reach —
   necessary, not sufficient, and self-update still must not be built first.
   *(Superseded 2026-09-23: self-update is approved without waiting for
   signing, and a signature becomes an added check once it exists.
   `spec-self-update.md` §10.)*
3. **A stronger managed-enrolment credential.** `spec-managed-distribution.md`
   §3.6 names two and recommends one: network-origin binding (cheap-looking and
   a trap — it rests on `X-Forwarded-For` trust and is a spoofable
   authorization if got wrong) and directory-vouched enrolment (a Kerberos
   machine-account proof or an Intune device attestation presented alongside
   the token). The second is a second trust anchor for device identity, which
   is structurally what invariant 1b governs for MDM, and it is what would make
   a leaked GPO token near-worthless.

4. ~~**`POST /enrollment/tokens` defaults `max_uses` to null**, and null means
   unlimited.~~ **CLOSED 2026-08-26**, by the kernel rather than by this module.
   Found here, recorded here as *found and not fixed* because a distribution
   feature may not change a kernel default, and then fixed where it belonged:
   `max_uses` on `POST /api/v1/enrollment-tokens` (note the hyphen — this spec
   wrote it `/enrollment/tokens` throughout and the route never had that shape)
   is now required and capped at `services/enrollment.MAX_TOKEN_USES`, and there
   is no longer any way to express the unbounded case on any route. The column
   stays nullable so that rows issued before the bound are grandfathered rather
   than rewritten.

   `spec-managed-distribution.md` §3.5's compatibility argument was right for
   what it governed and is now history rather than policy; the deployment-token
   route's own bounds (§3.3) are unchanged and remain the tighter pair.

5. **Version comparison against the fleet.** The console could show "12 endpoints
   are on an older version than the newest release". The data exists
   (`agents.version`); the screen is a small addition once releases are a
   concept.
