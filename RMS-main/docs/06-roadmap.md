# 06 — Roadmap

This platform is roughly ten modules on a shared kernel. It is built **one phase
at a time**, and each phase gets its **own** spec → implementation plan → build →
verify cycle. A single monolithic plan for the whole platform would collapse; a
kernel that modules register into will not.

## Build discipline (applies to every phase)

For each phase:

1. Read the relevant brief(s) in `docs/modules/` and the core docs.
2. Run the **brainstorming skill** to turn the scoping brief into a full spec.
3. Write the spec to `docs/modules/<module>/spec.md` (or `docs/kernel/spec.md`
   for Phase 0) and commit.
4. Run the **writing-plans skill** to produce the implementation plan.
5. Build **test-first** (test-driven-development skill); verify before claiming
   done (verification-before-completion skill).
6. Modules **register into the kernel** — agent capability, server job types, API
   routes, migrations, frontend feature folder — without forking core security,
   tenancy, or the job engine.

## Phase 0 — Platform Kernel (blocks everything)

No business module is built until the kernel exists. Deliverables:

- Repo scaffold (backend, frontend, deploy) per `CLAUDE.md` layout.
- PostgreSQL with **multi-tenancy + Row-Level Security** and the core data model
  (`docs/04-data-model.md`).
- **Keycloak** integration: SSO (OIDC/SAML, AD/Entra federation), MFA, and RBAC
  with baseline roles.
- **Agent enrollment**: internal CA (Vault PKI / step-ca), mTLS, certificate
  lifecycle.
- **Agent gateway**: outbound-only mTLS WebSocket, connection→device→org binding.
- **Signed-job engine**: sign, dispatch, track, retry; the agent-side signed-job
  runner and local scheduler.
- **Immutable, hash-chained audit log** + object-storage wiring (MinIO).
- **Minimal web shell**: login, device list, job status.
- Skeleton **agent** (`agent/`, in this repo): enroll, connect, run a signed
  `hello`/`inventory.collect` job, honor the idle budget. *Self-update was
  listed here and never built. It was refused as OQ-3 until 2026-09-23, when
  the operator approved it as a signed `agent.update` job with local rollback
  and ring rollout. It is now its own cycle:
  `docs/modules/agent-distribution/spec-self-update.md`.*
- **Agent distribution** (`backend/app/modules/agent_distribution/`, added
  2026-08-18): publish an artefact to the console and hand an operator a
  one-machine install command carrying a single-use enrollment token. It sits
  here rather than in a numbered phase because it is the other half of
  enrollment — how the binary reaches a machine that has no agent — and it
  dispatches no jobs of its own. **Explicitly not a push install:** the control
  plane never connects to an endpoint (invariant 2). See
  `docs/modules/agent-distribution/spec.md` §1.

- **Agent push install** (`backend/app/modules/agent_push/`, added
  2026-08-26): cause a machine that is not enrolled to run this platform's own
  agent installer, and nothing else, from inside its own network segment. It
  sits here beside agent distribution and not in a numbered phase for the same
  reason — it is another answer to "how does the binary reach a machine that
  has no agent", and it dispatches no business jobs of its own. It is governed
  by **invariant 2a** (`CLAUDE.md`): every packet reaching the target
  originates from a declared site server inside a signature-verified job, no
  operator-authored command travels, the push mints no identity, three
  independent authorizations must all hold, and the target set is explicitly
  named addresses a person selected — never a group, a filter or a subnet. The
  accounting the operator approved it on, including what it deliberately does
  not claim, is `docs/modules/agent-distribution/spec-push-install.md` §7.3.

**Exit criterion:** enroll a Windows, a Linux, and a macOS endpoint, dispatch a
signed job to each from the console, and see verified results — with agents idle
within footprint budget and no inbound ports on endpoints.

## Phase ordering (after the kernel)

Ordered by value, risk, and dependency. Rationale per phase noted.

> **STATUS, MEASURED 2026-08-14 — READ THIS BEFORE READING THE TABLE.**
>
> **All 18 phases below have shipped surface.** Every one has a backend
> package, routes mounted on the live router, tests that import it, and a
> frontend feature folder. **376 routes are mounted under `/api/v1`: 358
> defined across 25 backend modules, and 18 by the kernel itself
> (`app/api/v1/`).**
>
> (Those three numbers were wrong in this note's first draft — "376 across 24
> modules", which double-counted the kernel's routes as a module's and
> miscounted the modules by eye. They are now read off
> `test_roadmap_status.py`'s own measurement. Recording the slip because it is
> the identical mistake this note exists to describe, made while describing
> it.)
>
> **Until this note, exactly three rows carried a "BUILT" marker.** The table
> is a *plan*, and its rows were written as plan entries and mostly never
> updated — so anyone reading it to decide what to build next was reading a
> document that understated its own project by fifteen phases. That is the
> same defect this codebase has now found in itself three times: a status
> claim that nothing forces anyone to re-check. The others were the AI tool
> surface's "2 of 147" (stale in three documents at once) and the endpoint
> health module's six named-but-unrun falsifiers.
>
> **So the status is no longer carried by markers in this table.**
> `backend/tests/test_roadmap_status.py` pins it against the tree and fails
> naming the phase that lost a signal. It needs no database and runs anywhere.
> Per-row prose below may still describe original intent rather than current
> state; the test is the authority on what exists.
>
> **"Has shipped surface" is not "is finished", and the distinction is load
> bearing.** The test asserts four structural signals. It says nothing about
> whether a module's features are complete, its docs accurate, or its code
> ever executed on the platform it targets — `backup.run`, for instance, does
> not complete end to end (`scripts/verify_backup_run.py`), and no Windows or
> macOS agent code has ever run. Read a green row as "this exists and is
> wired in", never as "this works".
>
> **Six shipped modules appear in no phase at all**: `email_intake`,
> `notification`, `portal`, `reporting`, `site_server`, `sla`. Some may belong
> inside an existing phase's scope, but none is named. `reporting`'s own spec
> opens by saying so — *"There is no brief. No `docs/modules/reporting.md`, no
> roadmap phase."* — and it turned out not to be alone. That set is pinned by
> the same test, so a seventh cannot appear quietly.

| Phase | Module | Why here |
|---|---|---|
| 1 | **Inventory** | First real module; proves the agent→signed-job→result→UI loop end-to-end; highest immediate value, lowest risk; every later module depends on knowing the fleet. **Extended 2026-08-18 with agentless assets** (`docs/modules/asset-inventory/spec.md` part 1): a `devices` row need not have an agent, so warehouse stock and network gear live in the same list. It adds no backend package — the code is in `api/v1/devices.py` and `models/core.py` — which is why the unphased-module test is unaffected. Parts 2 (discovery) and 3 (enrolment matching) are not built |
| 2 | **Security logs (login/logout)** | Contained; event-driven collection + central ingest/search; produces SOC 2 evidence early |
| 3 | **Remote control** | High helpdesk value for a BPO; exercises the **credential vault** and **session audit/recording**; depends on vault + audit being solid |
| 4 | **Patch management** | Inventory-driven; per-OS; approval workflow; integrates **Sentinel** for prioritization; depends on inventory |
| 5 | **Application deployment** | Shares the per-OS package/install machinery with patch; natural follow-on |
| 6 | **Backup engine** | Heaviest module; build after the platform and agent are proven; v1 = file-level with the repository format pinned for later phases |
| 7 | **Provisioning & imaging** | Infra-heavy; needs the **site-server** role matured (PXE, caching); per-OS strategies |
| 8 | **IT documentation** | Low-dependency; can be pulled **earlier** if a lighter phase is wanted between heavy ones (see note) |
| 9 | **Change management** | Ties patch/deploy/provisioning into change records. Shipped with `ChangeTicketPort` + a null adapter, originally intended for **Odoo Helpdesk**; Phase 14 repoints it at the platform's own Service Desk (see that row) |
| 10 | **BCP test reports** | Depends on backup + change; structured DR/BCP test plans, execution records, evidence export |
| 11 | **Licenses management** | First of the financial trio (SAM); low-dependency (needs only Inventory); introduces the shared **`vendor`** entity the other two reuse; reconciles entitlements against installed software for compliance + renewal control |
| 12 | **Subscriptions management** | Second of the financial trio; SaaS/seat registry with renewal + cancellation-notice alerting; kernel-only dependency (reuses `vendor` from Licenses); no agent capability by design |
| 13 | **IT budget & expense** | Third of the financial trio; plan vs actual vs committed spend; **consumes Licenses + Subscriptions** as auto-feeds (renewals/billing cycles → expected expenses + committed-spend forecast); IT lens over spend, exports *to* the ERP (Odoo), never replaces it |
| 14 | **Service Desk** | The platform's own ticketing — incidents and service requests, queues and assignment, per-category request approval, device-linked tickets. **Reverses a former non-goal** (`docs/00-vision.md` previously deferred ticketing to Odoo Helpdesk); see `docs/modules/service-desk/spec.md` §0.1 for why. Built as **five sequenced sub-projects**, each with its own spec, plan, and review cycle: (1) ticket core, (2) notification core — outbound webhook + SMTP, shared with alerting, (3) end-user portal, (4) email intake, (5) SLA engine. Phase 16's alert→ticket loop can land any time after (2). Change management's `ChangeTicketPort`, stubbed with a null adapter since Phase 9, finally gets a real backend |
| 15 | **IP address management (IPAM)** | The address plan of record (phpIPAM/NetBox category) — subnet/IP registry with assignment workflow, **continuously reconciled** against agent-inventory NIC data + `net.sweep` discovery to surface drift/conflicts; low-dependency (needs only Inventory), so like documentation and the financial trio it can be **pulled earlier** — a strong candidate for an operation living off IP spreadsheets |
| 16 | **Endpoint health, alerting & auto-remediation** | **BUILT — control plane and agent; posture probes now exist on Linux, macOS and Windows (Linux verified against real system tools, macOS/Windows compile-checked only — see the status note below for exactly what per platform).** *(This cell read "the `health.check` agent capability is still unwritten, and its idle-budget falsifiers are still unmeasured by this repo's CI" until 2026-08-13 — already false by the time the status note below was corrected on 2026-08-12; corrected here to match.)* The proactive loop the module set was missing (Atera-parity gap): threshold-based edge health checks → alert → **ticket / signed-job remediation / notify**, with dedup, flap-suppression, and auto-clear. Adds one new, deliberately cheapest-possible agent capability (`health.check`, dormant between ticks, breach/recovery/summary only); remediation reuses existing job types. **Recommended real position is early-mid (right after Inventory, before/alongside Service Desk)** — it is the *signal source* Service Desk (alert→ticket) and the AI layer (alert→autonomous fix) consume, so it is a strong pull-forward candidate despite its default slot here. Hard boundary with the separate monitoring product: **no streamed metrics, no time-series history, no SNMP/topology** — "act on this device now" lives here; "analyze this trend" belongs to monitoring |
| 17 | **AI admin agent (platform-ops copilot)** | Third AI persona (with the end-user resolver + technician copilot specced in `ai-automation.md`) — a conversational agent that queries the whole estate and composes platform operations. **Governing principle: no powers the admin doesn't have** — runs as the authenticated admin, acts only through existing APIs + the signed-job engine, bounded by that admin's RBAC and the org's approval/change rules; every mutation is preview→confirm→execute and fleet-wide/destructive ops never auto-fire. **Split delivery: read/analyze mode ships early** (it is conversational reporting — pair it with Reporting & Analytics; read-only, RBAC-scoped) while **act mode rides the AI Automation substrate** (shared orchestration service, single approved-action registry with a persona/risk dimension, one autonomy dial, one AI audit story). Zero endpoint code |
| 18 | **AI Automation layer (the substrate the three personas share)** | **BUILT.** One orchestration service in the kernel (`app/core/ai/`) shared by the platform-ops admin, the technician copilot and the end-user tier-1 resolver, plus preview → confirm → execute over the existing `approved_action` catalogue. **Governing principle: the model names a catalogue KEY, never an effect** — job type, params, script pin, risk and rate fuses all come from a row an org admin authored, and the confirm carries an id and a digest and nothing else. Confirming additionally requires the permission the job type's OWN module dispatch route requires, so `ai.act.confirm` is not a skeleton key. One autonomy dial (`org × persona × risk class`) whose vocabulary stops one short of `autonomous` **by database CHECK**, and which can only ever LOWER what the catalogue permits. The persona that reads the most attacker-controlled text — the resolver, on unauthenticated email ingress — has the LEAST reach and proposes nothing at any dial position. Zero endpoint code: no agent capability, no job type, no wire-protocol change. Spec: `docs/modules/ai-automation.md` |

**Note on the financial trio (Phases 11–13):** Licenses → Subscriptions → Budget
form a dependency chain — Licenses introduces the shared `vendor` entity;
Subscriptions reuses it; Budget consumes both as auto-feeds for expected expenses
and the committed-spend forecast. They are **control-plane only** (no new agent
capability; Budget/Subscriptions add none, Licenses reuses `inventory.collect`),
which keeps them light relative to the agent-heavy Phases 4–7. Licenses and
Subscriptions are individually low-dependency and can be **pulled earlier** as
lighter builds between heavy phases (same latitude as IT documentation); Budget
can run standalone on manual entries but delivers its "unfair advantage" only
once its two auto-feeds exist. A shared **base-currency / FX** convention must be
decided in the Licenses spec and reused across all three.

**Note on ordering flexibility:** IT documentation (Phase 8) has almost no
dependency on other modules and is comparatively low-risk. It can be slotted
earlier — e.g. right after inventory — to give the team a lighter build between
the vault-heavy remote-control phase and the OS-heavy patch/deploy phases. The
ordering above is the default, not a constraint.

**Note on IPAM (Phase 15):** IP address management is **low-dependency** (Inventory
is the only hard requirement; site servers are optional sweep accelerators), so
like documentation and the financial trio it can be **pulled earlier** — for an
operation running off IP spreadsheets it is one of the strongest pull-forward
candidates. Its differentiator is the same reconciliation trick Licenses plays
with installed software: the agent fleet already reports every managed device's
NICs/IPs/MACs through inventory, so the documented address plan is continuously
checked against reality (drift, stale, mismatch, conflict). It adds one new agent
capability — **`net.sweep`** (a job-scoped, idle-budget-compliant ICMP/ARP
discovery sweep, dispatched preferentially to site servers, else a designated
per-subnet agent) — governed by per-subnet authorized-scope lists consistent with
Sentinel's scan governance; unauthorized segments stay observe-passive. It is
**not** live network monitoring (up/down, latency, SNMP performance polling, and
topology belong to the separate monitoring product); it is the plan of record and
its reconciliation. It **absorbs** the network/IP typed records sketched in the IT
documentation brief rather than duplicating them.

**Status of Phase 16 (Endpoint Health, Alerting & Auto-Remediation) — BUILT
control-plane side, NOT built agent side.** The control plane has shipped:
`docs/modules/endpoint-health-alerting/spec.md` and its plan, migrations
`0040`–`0042` plus `0044`, `backend/app/modules/health/`, the shared
`approved_action` registry in `app/core/approved_actions.py`, the four sweeps,
the action worker, 31 API routes, and `frontend/src/modules/health/`. A live
end-to-end run against a real Postgres and Redis exercises profile → assignment
→ signed policy push → `health_report` ingestion → alert dedup → the three
actions → ticket auto-resolution on recovery → the rate fuses and flap
suppression.

**What is NOT built, and it is the half that touches every endpoint.**
*Rewritten 2026-08-12 — the claim that stood here was false.* It read: "The
`health.check` agent capability **does not exist**. It is specified (§4, §5 of
that spec) and no line of it has been written."

It does exist. `agent/internal/capabilities/health/` holds the tick engine, the
closed check-type registry covering all fourteen types of §4.1, policy
application and the report path; `main.go` binds the capability and registers
both job types (`health.policy.apply`, `health.check.now`); and all six §0.5
falsifiers are tests that pass.

*Superseded 2026-08-13:* this paragraph used to read: "**What is genuinely
missing is narrower:** fast-tick sampling on Windows and macOS
(`sampler_other.go` answers `unavailable` for every check) and every posture
probe on every platform including `disk.smart_ok` (`posture.go` answers
`unavailable`). Both are argued refusals rather than omissions — Windows and
macOS need real counters instead of the fast-tick shell-out that §0.5
falsifier 3 forbids, and each posture probe is a footprint argument of its
own." The posture half stopped being true once the posture probes were built.

**What is genuinely missing, corrected:** fast-tick sampling on Windows and
macOS is unchanged — `sampler_other.go` still answers `unavailable` for every
check, because both platforms need real counters instead of the fast-tick
shell-out §0.5 falsifier 3 forbids. Posture is narrower now: only
`posture.antivirus_healthy` on Linux and macOS (§4.1 makes it Windows-only)
and `disk.smart_ok` on macOS (`—` in §4.1's table) are still `unavailable`.
Linux's posture probes run against real system tools; macOS's and Windows'
are implemented but have never executed on their target OS — no such machine
exists in this project, so they are compile-checked only. Because
`unavailable` is a stored, surfaced, countable state that never opens or
clears an alert (§0.7), a fleet on this build is not silently reported
healthy either way. See the spec's **fifth amendment** for the verified
detail, platform by platform.

*Corrected 2026-08-13:* this paragraph led with "It also **has no coverage in
this repo's CI**", which the rest of the paragraph then contradicts, twice. The
`health.check` capability's tests, including all six §0.5 falsifiers and the
five posture ones, run in the `agent` job on every push. What has no CI coverage
is narrower and worth keeping in view: **behaviour on Windows and macOS.** Those
files are type-checked for their `GOOS` and have never executed anywhere.

*Corrected 2026-08-12:* the text here claimed CI
"runs exactly three jobs (`backend`, `deploy`, `frontend`) and contains no Go
toolchain and no memory or allocation benchmark". That is false.
`.github/workflows/ci.yml` has **four** jobs; the fourth is `agent`, which pins
Go 1.24 and runs `gofmt`, `go vet`, `staticcheck`, `go test ./...`, the
idle-budget tests with their numbers printed, six cross-compile targets and the
packaging build. *Corrected 2026-08-13:* this said "It went green on
2026-08-12". Nobody has observed a green run in Actions — `HANDOFF.md` §6 says
so in the same breath — and the packaging `sudo`/`stat` blocker was found after
that sentence was written, so it was not green. What is true and was checked:
the two blockers that had kept the job red (a `GOOS=windows go vet` compile
failure and ten staticcheck findings) are cleared, and the job's steps run and
pass locally under the workflow's own commands. Whether the latest Actions run
is green is a thing to look at, not to assert.

*Corrected again, same day:* the sentence that stood here — "the `health.check`
capability has no coverage because no line of it exists to cover" — is **false**,
and so is the paragraph above it that this section opens with. It was repeated
from this file's own stale text while correcting a different stale claim in the
same edit. The capability IS implemented and wired
(`agent/internal/capabilities/health/`, both job types registered in `main.go`),
and all six §0.5 falsifiers exist as tests that pass and are matched by the CI
idle-budget step's `-run` pattern. *Superseded 2026-08-13:* this sentence used
to end "...and for what genuinely remains: fast-tick sampling on Windows and
macOS, and every posture probe, all of which answer `unavailable` today" —
the posture half stopped being true once the probes were built. See that
spec's **fifth amendment** for the verified state and for what genuinely
remains: fast-tick sampling on Windows and macOS (unchanged), and two
specific posture checks — `posture.antivirus_healthy` off Linux/macOS and
`disk.smart_ok` off macOS — that are `unavailable` by design (§4.1), not by
omission.

*Rewritten 2026-08-13.* This section used to close: "The idle-budget numbers in
that spec's §0.5 … are **a design target, not a measured result**, and so are
its six falsifiers", followed by "the falsifiers are unrun because nobody has
written the benchmark … neither exists yet". Both are contradicted by the
paragraphs above them, which were written in the same range and say the
falsifiers exist, pass, and are matched by the CI step's `-run` pattern. What is
true now:

- §0.5's six falsifiers are **eight Go tests that run on every push** in the
  `agent` job, and the idle-budget step prints their numbers into the build log.
  Five more (`posture_budget_test.go` A–E) cover the posture probes, which spawn
  processes and are a cost the original six do not measure. The numbers are
  measured on the CI runner's Linux, which is where the measuring happens.
- What remains a **design target rather than a measured result** is the budget
  on **Windows and macOS**. Not one line of `posture_windows.go`,
  `posture_darwin_probes.go` or `sampler_other.go` has executed anywhere; they
  are type-checked for their `GOOS` and nothing more. `CLAUDE.md`'s rule applies
  to them unchanged: until a capability's footprint has a benchmark that runs,
  its spec must call it a design target.
- The *configuration* half of the budget is enforced server-side regardless:
  §0.11's caps are database constraints, and `policy.go` restates them agent-side
  so a policy that would break §0.5 is refused rather than installed.

Read the phase as "the server half is done and tested; the endpoint half is
built and measured on Linux, and unexecuted on Windows and macOS".

**Note on Endpoint Health & Alerting (Phase 16):** its brief recommends an
**early-mid** real position — after Inventory, ideally before or alongside a
Service Desk module — because it is the proactive **signal source** that Service
Desk (alert→ticket) and the AI layer (alert→autonomous remediation) feed on, and
it is the one genuine gap surfaced by the Atera comparison. The default slot here
(16) is therefore a floor, not a recommendation: it is a strong pull-forward
candidate. Its non-negotiables are the **idle-budget** constraint on the
`health.check` tick (breach/recovery/summary events only, no resident metric
history) and the **hard boundary with the separate monitoring product** (no
time-series, SNMP, or topology — those belong to monitoring). Alert-triggered
remediation is **always a signed, audited job** gated by the same approved-action
registry + autonomy dial as the AI layer; nothing destructive or fleet-wide
auto-fires.

**Status of Phase 17 (AI admin agent) — READ MODE BUILT. ACT MODE BLOCKED, NOT
MERELY UNBUILT.**

Read/analyze mode has shipped: `docs/modules/ai-admin/spec.md` and its plan,
migration `0045`, `backend/app/modules/ai_admin/` (the static tool registry, the
in-process loopback executor, the fail-closed scrubber, the nonce envelope and
injection heuristic, the bounded turn loop, persistence with the `ai.*` audit
events, eleven API routes with an SSE side-channel, and two `httpx`-only model
provider clients), and `frontend/src/modules/ai-admin/`. An administrator asks a
question in the console and the agent answers by calling allowlisted `GET`
routes **as that administrator**, through the whole ASGI stack, with the
caller's own token — so `require_permission` and RLS run verbatim and the agent
has no power the admin does not already have.

**AMENDED: ACT MODE IS SPECIFIED AND BUILT.** The paragraph this replaces said
act mode was blocked on `docs/modules/ai-automation.md`, which had never been
written. It has been written, and the layer it specifies has been built — see
**Phase 18** below, which is the phase number this roadmap owed it.

§12 of the ai-admin spec is now a record of the seam this module reserved rather
than the authority on act mode; the authority is `docs/modules/ai-automation.md`.
Its own §18 and §19 record what the two halves of the build learned, including
the defects they found in code that had already shipped.

What the admin persona gained: `may_propose=True`,
`replay_policy="neutralize_flagged"`, a `mode` field on `POST /ai/sessions` and
four act routes — every one of which DELEGATES to `app/core/ai/act.py`. The
boundary test that used to assert the spec file's absence was replaced (not
deleted) by one asserting the guarantee that outlives it: act mode is not GROWN
inside `app/modules/ai_admin/`.

**The v1 tool registry is TWO tools, and that number is the measurement, not the
shortcut.** Spec §0.3 sets mechanical properties a route must satisfy before it
may be exposed.

> Measured when this module was built: **147 `GET` routes under `/api/v1`; 0
> carry `summary=`; 1 `Field(description=...)` across every module's
> `schema.py`; 13 routes carry an endpoint docstring; 2 routes satisfy all five
> properties.**

**RE-MEASURED 2026-08-14: 188 `GET` routes; 10 satisfy properties 1–5; 3 satisfy
all six.** The registry is still two tools, and the reason changed from "nobody
has done the work" to something more interesting.

**§0.3 gained a sixth property, `closed`, because five were never sufficient.**
A route can be perfectly documented and still be impossible to expose: the AI
layer's scrubber requires that every key a response can carry be knowable from
its model in advance, and `ReadTool.response_fields` is pinned *equal* to the
model's field names. A response model containing a `dict` — whose keys are data
— therefore has **no legal declaration**. Nor does one with a field named
`*_key`, which the secret-name pattern blocks outright. Reporting shipped five
routes that pass properties 1–5 *by construction* and **not one of them can be
registered**. That is the news, and it went unnoticed for a phase because the two
routes already registered happen to satisfy the sixth property by accident.

**Every module that wants its data reachable by the agent has a specific,
checkable bill — now six items:** a `summary=` on the route, a typed
`response_model`, a `Field(description=...)` on every field of it, a `limit` on
any list route, no `dict` anywhere in the response model, and no field name
containing a secret word. Paying only the first four is not enough; reporting
paid them in full.

`backend/tests/test_ai_tool_surface.py` pins the whole measurement against the
live router — route count, both pass counts, and the ungated routes — and fails
naming exactly which number moved. It needs no database, so it runs anywhere.
`test_every_registered_tool_passes_the_six_properties` remains the per-tool gate,
and the two share one implementation of the properties rather than two.

**One closure worth stating:** the third route passing all six is
`GET /api/v1/ai/status`, and the registry already bars every `/api/v1/ai` path as
a recursion primitive. So the set of routes that *could* be registered today is
exactly the set that *is* — there is nothing to add until a module pays the bill.

**Note on the AI admin agent (Phase 17) and the AI layer:** this brief is the
third persona of an **AI Automation layer** specced separately in
`docs/modules/ai-automation.md` (the end-user tier-1 resolver + technician
copilot). **AMENDED: that layer is now Phase 18 and is built** — the sentence
this replaces said it was "not yet phased". Consequently the agent
splits cleanly: its **read/analyze mode is standalone and can ship early** —
it is conversational reporting over each module's existing query APIs, read-only
and RBAC-scoped — while its **act mode is a hard dependency on the AI Automation
substrate** (orchestration service, the single `approved_action` registry, the
autonomy dial, AI audit) and cannot precede it. Do **not** build a second AI
stack: personas share one orchestration service, one registry (with a
persona/risk dimension), one dial, one audit story. Prompt-injection hardening is
a spec-level requirement — all retrieved user/device text is **data, never
instructions**, and tool invocation is constrained to the registry regardless of
what retrieved text says. A useful side effect: this persona is a forcing
function for clean, well-documented module APIs, since every module's API doubles
as the agent's tool surface.

## MVP definition

**Phases 0–3** constitute a genuinely usable internal RMM: enrolled fleet,
live inventory, centralized security logs, and audited admin remote control with
vaulted credentials. This is the point at which the platform earns its keep
internally, before the heavier patch/deploy/backup/imaging investments.

## Commercialization gate

Pursue commercial packaging only after the internal deployment proves out and
these hold (from `docs/00-vision.md`):

- Multi-tenant isolation verified under real multi-org data.
- Cloud deployment path exercised (control plane in cloud, site servers on
  customer LANs).
- Backups with **proven restores**, not just schedules.
- Clean open-core licensing boundary confirmed (no copyleft contamination in
  agent, remote-control, or backup paths).
- SOC 2 evidence exports demonstrably useful.

## Cross-cutting workstreams (run alongside, not as separate phases)

- **Security hardening & pen-testing** of the kernel before Phase 3 (remote
  control) ships, since that phase unlocks live privileged sessions.
- **Site-server role** matures incrementally: caching (Phases 4–5), then PXE/
  imaging (Phase 7).
- **Integrations** land with the module that needs them: AD/Entra SSO in Phase 0,
  Sentinel in Phase 4, Odoo Helpdesk in Phase 9, evidence/BI exports as modules
  produce evidence.
