# CLAUDE.md — Operating Guide for Claude Code

This file is the contract for every build session on this project. Read it in
full at the start of each session. It defines what the project is, how the repo
is organized, the invariants you must never violate, and the discipline for
building modules.

---

## What this project is

A modular IT management / endpoint control platform (RMM + endpoint management +
IT-governance workflows). One control plane, one lightweight agent per endpoint,
optional site servers for LAN-heavy work. Internal-first for Toeshee, designed to
be commercialized. On-prem now, cloud-ready later.

If you need product context, read `docs/00-vision.md`. For architecture, read
`docs/01-architecture.md`. Do not re-derive decisions already recorded in
`docs/` — treat those docs as authoritative and change them deliberately if a
decision needs to evolve.

---

## Repository layout (target)

This repo holds the **control plane, the agent, and the docs**. The agent is
still **not** shared with the monitoring platform.

```
it-suite/                     # this repo
├── CLAUDE.md
├── README.md
├── docs/                     # authoritative design & specs
├── backend/                  # FastAPI application
│   ├── app/
│   │   ├── core/             # config, security, db, tenancy, audit
│   │   ├── gateway/          # agent WebSocket gateway + job engine
│   │   ├── modules/          # one package per business module
│   │   └── api/              # HTTP routes (versioned)
│   ├── migrations/           # Alembic
│   └── tests/
├── frontend/                 # React + TypeScript + Vite
│   ├── src/
│   │   ├── modules/          # one feature folder per business module
│   │   ├── components/
│   │   └── lib/
│   └── tests/
├── agent/                    # the Go agent
│   ├── cmd/agent/
│   ├── internal/
│   │   ├── transport/        # mTLS WebSocket client
│   │   ├── enroll/           # enrollment + cert lifecycle
│   │   ├── scheduler/        # local cron-like scheduler
│   │   ├── jobs/             # signed-job runner + verification
│   │   └── capabilities/     # one package per capability (inventory, patch, ...)
│   └── packaging/            # windows service, systemd, launchd installers
├── deploy/                   # docker-compose (on-prem), helm/ (cloud path)
└── scripts/
```

### The agent moved in-tree, and two things must replace what repo separation gave us

The agent used to live in a separate `it-suite-agent` repository. That
separation is gone; **the reasons it existed are not**, and nothing else
enforces them now.

1. **The wire protocol is still a contract, not an internal call.** A running
   fleet is always mixed-version: an agent in the field is older than the
   control plane it talks to, and updates on its own schedule. Living in one
   repo makes it *possible* to change both sides in one commit and *tempting*
   to treat the envelope as an internal detail. It is not. Every change to
   `docs/kernel/wire-protocol/` is a compatibility decision — additive, or
   versioned, or it breaks deployed agents. A field that a new control plane
   requires and an old agent does not send is an outage, and the test suite
   will not show it to you.
2. **A green backend suite says nothing about the agent.** Go code needs its
   own CI job. The `health.check` idle budget in
   `docs/modules/endpoint-health-alerting/spec.md` §0.5 shipped with six named
   falsifiers and **zero** coverage, precisely because the code they measure
   was in a repo this CI could not see. In-tree, that excuse is gone: the
   budget is measured by a benchmark that runs, or the number is not claimed.

Agent work now belongs here. Write it under `agent/`, and hold it to the same
standard as the rest: tests first, no unsigned command path, and the idle
budget below verified rather than asserted.

---

## The security invariants (NEVER violate these)

This platform is **domain-admin-equivalent command-and-control over an entire
fleet**. It is a crown-jewel target. The following are not features to be traded
off for convenience — they are invariants. If a task appears to require breaking
one, stop and surface it rather than implementing around it.

1. **Every job the agent executes must be cryptographically signed by the server
   and verified by the agent before execution.** No unsigned command path exists,
   ever — not for testing, not for a "quick" internal tool. See
   `docs/03-security-model.md` and `docs/05-agent-spec.md`.
2. **Agents connect outbound only, over mutual TLS.** The server never opens an
   inbound connection to an endpoint. No agent listening port.
3. **Stored credentials (remote-control admin creds, provisioning creds) live
   encrypted in the vault and are only decrypted at point of use.** They are
   never written to logs, never returned in API responses, never stored in the
   application database in plaintext.
4. **Every privileged action is written to the immutable, hash-chained audit
   log** — job dispatch, credential access, remote-session start/stop, config
   change, policy change. The audit log is append-only and tamper-evident.
5. **All data access is tenant-scoped.** Every query is constrained by `org_id`,
   enforced at the database layer via Row-Level Security, not only in
   application code. A missing tenant filter is a security bug, not a style nit.
6. **RBAC is checked on every privileged endpoint and every job dispatch.**
   Authorization is server-side; the frontend never gates security.

**5a. Dentro de una organización, todo dato de un cliente está limitado a los
clientes del alcance de la sesión, y eso lo impone la base de datos mediante
RLS, no solo el código.** *(Approved by the operator 2026-10-23.)* Un técnico
limitado a ciertos clientes no puede leer ni escribir filas de otro. Una sesión
sin alcance ve cero filas. Solo los procesos internos usan el alcance "todos", y
cada uso está en una lista revisada.

   *Fase 2 (completada el 2026-10-29):* la base de datos impone 5a en toda
   tabla C, H u O de `client_scope_catalog.SCOPED`
   (`backend/app/core/client_scope_catalog.py`); ninguna familia queda
   pendiente. Los registros que guardan datos de todos los clientes — el de
   auditoría (tabla sin cliente, con un `client_id` informativo), el de correo
   entrante (cada mensaje nuevo con su cliente) y el de entregas de
   notificaciones (tabla sin cliente) — solo se leen con el permiso en todos
   los clientes. La instalación remota (2a) queda además dentro de un cliente:
   el activo avalado, la autorización, la credencial y el site server son del
   mismo cliente.

**1b. A device managed over a third-party management protocol (Apple MDM,
Android EMM) is commanded over that protocol's trust model, never over the
signed-job engine, and the two paths never meet.** *(Added 2026-08-16, on the
operator's approval of `docs/modules/mdm/spec.md`. Invariant 1 above is
unchanged — not one word. This clause governs a second path; it does not
qualify the first.)* All of the following are required:

   a. **No platform-authored executable payload.** The MDM path may carry
      declarative configuration and references to applications signed and
      distributed by the platform vendor's own store. It may not carry an
      installer, a package, a script, a binary, or any payload authored by
      this platform that the device will execute. Software installation on a
      device that also runs the agent stays on the signed-job path.
   b. **Disjoint identities, disjoint trust anchors.** MDM device identity
      certificates are issued by a dedicated intermediate CA that the agent
      gateway does not trust, and agent certificates by an intermediate the
      MDM ingress does not trust. An MDM command may target only a device
      carrying an `mdm_device` row; a signed job only one carrying an
      `agents` row.
   c. **Device identity is proven, never asserted.** Enrollment issues a
      per-device credential through a single-use, time-bounded, per-device
      challenge. No static shared secret may authorize that issuance.
   d. **Commands are signed before dispatch and verified before
      transmission.** Every MDM command is signed over its canonical bytes
      with the platform's job-signing key and re-verified by the dispatcher
      immediately before it reaches the vendor transport. A command whose
      signature does not verify is never transmitted.
   e. **Same authorization and same audit chain.** Every MDM command is
      RBAC-checked server-side, tenant-scoped, and written to the immutable
      hash-chained audit log identically to job dispatch.
   f. **The destructive subset is gated separately.** Remote lock and remote
      erase require their own permission codes, cannot be expressed against a
      group or a filter, and are never reachable by any automated or
      AI-driven actor.

   **What 1b deliberately does not claim.** The device does not verify this
   platform's signature. Invariant 1's endpoint-side guarantee — that a
   compromised control plane cannot make an endpoint act — **does not hold on
   this path**, and no wording makes it hold on a protocol Apple controls. It
   is replaced by (a): a path that cannot deliver code has a materially
   smaller worst case than one that can. The spec's §1.5 does that accounting
   in full, and §1.9 states that without this amendment the module is not
   built.

**2a. A machine that is not enrolled may be caused to run this platform's own
agent installer, and nothing else, and only from inside its own network
segment.** *(Approved by the operator 2026-08-26, on the accounting in
`docs/modules/agent-distribution/spec-push-install.md`. Invariants 1 and 2
above are unchanged — not one word. This clause governs a path to machines
that are not yet endpoints; it does not qualify either.)* All of the following
are required:

   a. **The control plane never connects to the target.** Every packet that
      reaches an unenrolled machine originates from a device the operator has
      declared a site server for that machine's segment, acting inside a
      signature-verified job. Where no such site server exists, the action is
      refused and recorded; there is no fallback originator.

   b. **No operator-authored or platform-authored command travels.** The
      signed parameters carry one target address, a transport, a release
      identified by version and SHA-256, a single-use enrollment token and a
      credential. They carry no command line, script, interpreter, argument
      string, package name or artefact URL of anyone's choosing. The only
      thing that executes on the target is the platform's own published agent
      artefact, placed by an installer compiled into the agent.

      *Two more fields, approved by the operator 2026-09-29 for Windows push
      over WinRM:* **the target's DNS name**, because Kerberos authenticates a
      machine by name and never by address; and **the console's enrollment
      URL**, taken from server configuration (`ITS_PUBLIC_CONSOLE_URL`) and
      never from a request, so the installed agent knows where to enroll. Both
      are data the installer reads. Neither is executed, and neither can name
      what runs.

   c. **The push mints no identity.** It delivers a single-use, time-bounded
      enrollment token. A device identity is issued only by the ordinary
      enrollment path, to a keypair the target generated itself and whose
      private half never leaves it.

   d. **Three independent authorizations must all hold**: the target is an
      asset row in this organization **that a person vouched for**; an active
      push authorization covers its address; and the credential chosen is
      scoped to that address. Any one absent is a refusal recorded before a
      signature is produced.

      *"Vouched for", not "promoted" — and the distinction was not academic
      for a single day.* This clause was approved on the morning of
      2026-08-26 reading "a promoted asset row", because promotion was then
      the only way an asset row came to exist without a person typing it. That
      afternoon the operator ruled that a sweep records what it finds without
      anyone selecting it, and the word stopped meaning what it was chosen to
      mean: the coffee machine now has an asset row too. A check written to
      the original wording would have presented an administrative credential
      to whatever answered a ping. The binding property is the warrant, so the
      permitted set is an allowlist — `agent`, `operator`, `promoted` — and a
      row a sweep recorded on its own is refused until a person claims it.
      Anything added to `devices.provenance` later is refused by silence,
      which is the correct direction for this particular list to be wrong in.

   e. **Same RBAC, same audit chain**, under permission codes of its own, and
      never reachable by any automated, scheduled or AI-driven actor.

      **2a(e), added 2026-09-30.** A push run the operator confirmed may be
      continued by the platform's pacer, which dispatches the confirmed
      targets as capacity allows. The pacer cannot start a run, add a target,
      widen a list, choose a credential, or resume a paused or cancelled run.
      Starting, pausing, resuming and cancelling are a person's acts,
      RBAC-checked and audited. The pacer's one act of its own is a safety
      stop: after repeated authentication failures it stops the run so a
      credential is not locked out, and only a person may restart it.

   f. **The destructive property is bounded, not removed.** A push may be
      expressed only against explicitly named addresses a human selected —
      never a group, a filter, a subnet or a saved search — and never more
      than the configured batch ceiling in one act.

      **2a(f), added 2026-09-30.** A subnet may be used to *build* the list a
      person confirms, never as the thing confirmed. The act is the
      confirmation of the named machines the subnet expanded to, bound by a
      digest of that list. Only rows vouched for under (d) are ever listed. A
      run holds at most 1,024 named targets, at most 50 of which are in flight
      at once. A push that is not a run keeps the batch ceiling of 25.

   **What 2a deliberately does not claim.** The platform cannot authenticate
   the target before presenting a credential to it; on first contact there is
   no host key, no certificate and no prior relationship. An attacker who
   controls the address receives an administrative credential, and on Windows
   may relay it. That is a permanent property of every credentialed push
   install, it is not fixed by any clause above, and the module's §7.3 does
   the accounting in full. It is bounded by (d), by credential dedication and
   by prefix scoping — not eliminated.

   **1b is not a precedent for this and is not cited as one.** 1b survives on
   clause (a), *no platform-authored executable payload*, which is what buys
   it "a materially smaller worst case than one that can". This path is
   nothing *but* an executable payload. All 2a borrows from 1b is its
   structure.

**1c. An AI connected over the platform's MCP server acts as the person who
connected it, and may reach what that person may reach — including acts that
1b(f), 2a(e) and the self-update, uninstall and screen-share specs reserve to a
person.** *(Approved by the operator 2026-10-09. Invariants 1, 2 and 3 are
unchanged.)* All of the following are required:

   a. **The session is the person, and only OAuth makes one.** A token issued
      by the platform's identity provider to that person after their own
      sign-in, carrying the MCP audience and no other platform audience. RBAC,
      tenancy and audit are identical to the console's.

   b. **Every MCP call is audited** with the channel `mcp`, the registered AI
      client and the tool.

   c. **No plaintext secret reaches an MCP response.** Credential checkout,
      license keys, enrollment tokens, install-link codes and setup codes are
      never tools, the API refuses them on the MCP channel, and the scrub
      blocks rather than redacts.

   d. **Off until enabled.** MCP access is off for an organization until an
      org-admin enables it. A person needs `mcp.use`, and every registered AI
      client can be revoked, with effect on its next call.

   e. **The API's own bounds still hold.** Named addresses and batch ceilings
      (2a(f)), no group target for lock or erase (1b(f)), the push lockout
      guard.

   **What 1c deliberately does not claim.** The platform cannot tell whether a
   person or the AI app approved a call: approval happens in the AI client,
   which the operator chose on 2026-10-09. A compromised AI client, or text in
   a ticket that the client acts on, holds the person's full authority. That
   is bounded by their RBAC, their per-tool settings in the client, the audit
   trail and revocation — not eliminated.

---

## Build discipline

This is ~10 modules on a shared platform kernel. **Do not attempt to build the
whole platform in one plan.** The order is fixed in `docs/06-roadmap.md`:

1. **Phase 0 (Platform Kernel) comes first and blocks everything.** No business
   module is built until enrollment, the signed-job engine, tenancy, RBAC,
   audit, and the agent gateway exist and a device can run a signed job
   end-to-end.
2. **Each module is its own cycle.** When starting a module:
   - Read that module's brief in `docs/modules/`.
   - Run the brainstorming skill to turn the brief into a full spec (the briefs
     are intentionally scoping-level, not implementation-level).
   - Write the spec to `docs/modules/<module>/spec.md`.
   - Use the writing-plans skill to produce an implementation plan.
   - Build test-first, then verify before claiming completion.
3. **Modules register into the kernel; they do not fork it.** A module adds:
   agent capability handler(s), server-side job types, API routes under its
   package, DB migrations for its own tables, and a frontend feature folder. It
   does not modify core security, tenancy, or the job engine except through
   their defined extension points.

---

## Engineering conventions

- **Backend:** Python 3.12, FastAPI, SQLAlchemy 2.x (typed), Pydantic v2, Alembic
  for every schema change. Async throughout. Type hints are mandatory; run the
  type checker. Business logic lives in services, not in route handlers.
- **Frontend:** React + TypeScript (strict), Vite, TanStack Query for server
  state. No `any`. Components small and focused.
- **Agent:** Go, single static binary, minimal dependencies. Nothing that busies
  the CPU when idle (see idle budget below). Cross-compiles to Windows, Linux,
  macOS. Lives under `agent/`, has its own CI job, and its tests run on every
  push — a control-plane change that breaks the agent must go red here, not in
  the field.
- **Tests first.** Follow the test-driven-development skill. New job types and
  new API endpoints ship with tests. Security-relevant paths (signing, auth,
  tenancy) require explicit tests.
- **Migrations are forward-only and reviewed.** Never edit a shipped migration;
  add a new one.

  **One carve-out, approved 2026-08-16, and it is narrower than it looks.** The
  rule exists to protect the **upgrade** path: a deployed database has already
  run `upgrade()`, so changing it rewrites history that machines in the field
  have executed. A `downgrade()` that has never run anywhere carries none of
  that weight. `0001_core_schema.py`'s `downgrade()` was corrected in place
  (it left the `job_status` enum behind, so `downgrade base` exited 0 and the
  next `upgrade head` died) after all three of these held:

  1. the edit touches `downgrade()` only, never `upgrade()`;
  2. the `downgrade()` has demonstrably never executed — CI did not run one,
     and production never will, because rollback is restore-from-backup
     (`docs/superpowers/specs/2026-08-15-deploy-rollback-design.md` D1);
  3. a forward migration **cannot** express the fix, because the statement has
     to run at that migration's point in the chain.

  Fewer than three means write a new migration. The reasoning is repeated at
  the edit site, and CI's `migrations` job now runs
  `upgrade head → downgrade base → upgrade head` so the class of defect that
  motivated this is caught rather than argued about.
- **Secrets never in code or git.** Config via environment / secret store.

## The agent idle budget (a hard requirement, not a goal)

The agent must be near-invisible when nothing is happening. Design target:
**low-tens-of-MB resident memory and ~0% CPU at idle.** This means:

- One long-lived connection parked on a blocking read — no polling loop.
- Scheduled work driven by timers, not spin.
- Heavy capabilities (patch, backup, remote, imaging) stay dormant and only
  allocate/initialize when a job for them actually runs, then release.
- No capability is "always on" unless the module brief explicitly justifies it.

Any change that regresses idle footprint is a defect. See `docs/05-agent-spec.md`.

**Measured, not asserted.** The numbers above are a claim about running code, so
they are owed a benchmark that runs in CI and a stated falsifier — what
observation would show the budget broken. Until a capability has that, its
footprint is a design target and must be described that way in its spec, not
reported as verified. This is not hypothetical: `health.check`'s six falsifiers
sat unrun for an entire phase because the code lived where CI could not reach
it.

---

## Reporting to the operator

**Every change that reaches the deployment ends with the full deploy commands.**
Not a diff of them, not "same as last time, plus `keycloak-init`" — the whole
sequence, copy-pasteable, in order, with what to verify afterwards. The
operator asked for this on 2026-08-20 and the reason is structural: they run
those commands on a host this session cannot see, so a partial list is a
guess about which state their host is in. State explicitly when a step is a
no-op for this particular change (no new migration, no new `.env` keys) rather
than dropping it from the list — a step that is missing reads as a step that
was forgotten.

**The operator always deploys from `main`, never from a feature branch.**
(Stated 2026-09-11.) So a deploy sequence that starts with `git checkout
<feature-branch>` is not a deploy sequence they can run — the work has to be
merged first, and the commands have to say so rather than assuming it. Every
report ends with the path from where the work actually is to a deployed
`main`.

**A copy-pasteable command contains no placeholders.** (Learned the hard way
on 2026-09-11.) The sequence said
`echo "IT_SUITE_VERSION=<sha printed>" >> .env`, the operator pasted it, and
`.env` ended up holding the literal string `<sha printed>` — `docker compose`
then failed with `invalid reference format`. "Copy-pasteable" is not a style
note: the operator runs these on a host this session cannot see, so anything
that needs substituting is a trap, not an instruction. Either give a command
that computes the value itself, or make the substitution step its own numbered
line that cannot be mistaken for something to run verbatim. A line that deletes
any prior key before appending is better still, because a second
`IT_SUITE_VERSION=` in `.env` silently wins over the first.

**A rollback target has to already exist to be one.** `build-images.sh` prints
`Recent tags:` and on a host that has never run it, that list is `(none yet)` —
meaning the "set `IT_SUITE_VERSION` to a previous SHA" rollback does not exist
for that deploy. Check it before describing rollback, and when there is no
prior tag, say plainly that restore-from-backup is the only way back rather
than repeating the general advice.

**Two steps are conditional, not optional, and the difference matters.**

- **OpenBao is sealed after every restart and every host reboot**
  (`deploy/GO-LIVE.md` §4), and a sealed OpenBao makes `docker compose up -d`
  *block* rather than fail — `openbao-init` waits on a healthcheck that only
  passes when unsealed. Whether it is sealed right now depends on the host's
  history, which no session can see. So the sequence always begins with
  `docker compose exec openbao bao status`, and never reports the unseal as a
  no-op on the grounds that this particular change did not touch OpenBao. It
  is not the change that seals it.
- **`migrate` runs as a dependency of `api` on every `up -d`.** Saying "no
  migration in this change" is a statement about what it will find to do, not
  a step to omit.

---

## When in doubt

- A decision already in `docs/` wins; change the doc deliberately if needed.
- A security invariant wins over convenience or speed, always.
- If a task seems to require violating an invariant or exceeding the agent idle
  budget, stop and raise it rather than working around it.
