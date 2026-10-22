# Module Spec — AI Automation layer (the substrate the three AI personas share)

> The layer `docs/06-roadmap.md` names as the hard dependency of Phase 17's
> **act mode**, and as the home of two further personas — the **end-user tier-1
> resolver** and the **technician copilot**. `docs/modules/ai-admin/spec.md` §12
> defers act mode to this document by name; until this document existed, act
> mode was blocked on a *specification*, not on implementation effort.
>
> Control-plane only — **no agent capability, no new job type, no new signed-job
> path.** Every act this layer can perform is an existing job type, dispatched
> through the unmodified `JobEngine`, from an `approved_action` row an org admin
> already authored.
>
> **This layer adds no powers.** It is a proposal-and-confirmation surface over
> machinery that already exists: the kernel `approved_action` registry
> (`app/core/approved_actions.py`), the signed-job engine, the hash-chained
> audit log, and each module's own dispatch routes. If a capability is wanted
> that no human on this platform has, it does not belong here.
>
> **Reading order.** §0 is the argument; §1–§9 are the design; §10 lists the
> kernel changes this layer requires (there are three, and they are small); §11
> lists what is **blocked on something that does not exist yet** and must not be
> specified as if buildable.

---

## 0. Resolved design decisions

Twelve decisions. Each rules out a family of designs, so they are recorded
before the design rather than discovered inside it. Each states the rejected
alternative and the failure mode or perverse incentive the choice accepts.

Every claim below about existing code was checked against the code. Where the
scoping material and the code disagreed, the code won and §0.13 says so.

### 0.1 One orchestration service, relocated to `app/core/ai/`; a persona is a frozen profile, not a package

**Decision.** The turn loop, the provider clients, the untrusted envelope, the
scrubber, the injection heuristic, the loopback executor, the transcript store,
the retention sweep and the audit writer become **kernel code** under
`app/core/ai/`. A persona contributes exactly three things and nothing else:

1. a `PersonaProfile` — a **frozen constant compiled into the image** naming its
   system prompt, its read-tool set, whether it may mint act proposals, and
   which principal resolver it runs under;
2. a router mounted on its own surface (`/api/v1/ai`, `/api/v1/portal/ai`,
   `/api/v1/service-desk/tickets/{id}/ai`);
3. the frontend feature folder that renders it.

The boundary in code is exact: **anything that touches a model provider, a
retrieved payload, or the audit log lives in `app/core/ai/`. Anything that
knows a persona's name lives in that persona's module.** `app/core/ai/` imports
no module package; a persona module imports `app.core.ai` and its own domain.

**Rejected: a persona package per persona**, each owning its own orchestrator —
which is the roadmap's named failure mode ("do not build a second AI stack")
arriving as three of them. It produces three copies of the scrubber, three
egress paths, and three answers to "may this run without a human".
`tests/test_ai_admin_boundaries.py` currently asserts that the AI module has
**exactly two HTTP-client construction sites**; three persona packages make that
assertion meaningless by making it local.

**Rejected: leave the orchestrator in `app/modules/ai_admin/` and have the other
two personas import from it.** That makes one business module a de facto kernel
that is not reviewed as one, and it inverts the dependency direction every other
module obeys. It is also how `approved_action` was nearly built — the kernel
registry's own docstring records the reasoning for putting it in `app/core/`
instead, and this is the same argument about the same problem.

**The cost, accepted and stated.** The relocation is a **mechanical move of
shipped, tested code**, and the security guarantees that currently live in
`tests/test_ai_admin_boundaries.py` are anchored to `PACKAGE_ROOT =
app/modules/ai_admin/`. Moving the code without moving those tests silently
un-asserts the egress-site count, the "no `mutating=True`" grep, the forbidden-
gate list and the `_NEVER_A_TOOL` set. The move is therefore **one task, and its
definition of done is that the boundary suite scans the new root and its
assertions are widened to cover every persona package as well** — otherwise the
first act-mode task is also the task that quietly deleted read mode's proof.

### 0.2 The end-user resolver is not a smaller admin agent — it runs on a different principal type and cannot use the loopback executor

**Decision.** The resolver serves a **portal principal**. `app/modules/portal/
principal.py` deliberately makes `PortalContext` *not* an `AuthContext`, and
portal reads run under `portal_session`, which pins **two** GUCs
(`app.current_org`, `app.current_requester`) and is fail-closed if either is
unset. A portal user holds `portal.ticket.read`/`portal.ticket.write` and **no
staff permission**.

Therefore the resolver **may not use `executor.invoke_tool`**. That executor's
entire security argument is that it carries *the caller's own bearer token*
through the whole ASGI stack so `require_permission` re-runs verbatim. Handed a
portal principal's token, every staff route returns 403 — so any resolver built
on it would either be useless or would be "fixed" by giving it a service token,
which is the privilege-escalation primitive the executor's docstring warns
about in as many words.

The resolver gets its **own executor and its own tool set**: the portal's
existing routes, invoked in-process under `portal_session` with the requester
pinned. Its entire read surface is *the requester's own tickets and their public
history* — the same rows the portal UI already shows that person.

**Rejected: one executor with a "principal kind" branch.** A branch is how the
staff path and the portal path end up sharing a code path that one of them was
not audited for. Two executors, two types, no branch: the resolver's executor
cannot construct a staff request because it has no `AuthContext` to build one
from.

**The failure mode accepted.** The resolver is genuinely weak. It cannot look up
the requester's device, cannot see fleet state, cannot read a runbook (§11.2),
and will frequently have to say "I have raised this with a technician." That is
correct for the persona that reads the most attacker-controlled text on the
platform, and §0.9 makes the inversion deliberate rather than incidental.

### 0.3 The technician copilot drafts; it never posts, assigns, or resolves

**Decision.** Inside a ticket, with the technician's own RBAC and the same
loopback executor the admin persona uses, the copilot may **read** (the ticket,
its thread, its linked device's state, the org's estate to the extent the
technician's permissions allow) and may **produce two kinds of output**: prose
answers, and **drafts** — a suggested public reply, a suggested internal note, a
suggested category/queue/priority. A draft is returned to the technician's
editor. It is never written to `ticket_comment`, never sets an assignment, never
moves a status.

**Rejected: let the copilot post a comment as the technician** (it holds
`ticket.write`, so it *could*). Two reasons, either sufficient. First, the SLA
engine's response clock stops at "the first public comment from someone who is
not the requester" (`docs/modules/sla/spec.md` §0.6), and §0.6.1 already had to
carve out machine-generated public notes because a single change transition
would otherwise auto-satisfy the response promise of every linked ticket. A
copilot that can post is a metric-laundering machine with a natural-language
interface. Second, a posted comment is customer-visible output attributed to a
named human who did not write it.

**Rejected: a `author_type='ai'` comment kind.** That is this layer defining
shared ticket structure, which the ticket core's modules are forbidden from
doing, and it re-opens §0.6.1 for a third author type.

**The perverse incentive accepted.** Drafting is one click short of posting, and
technicians will paste drafts unread. This layer does not pretend otherwise: a
pasted draft is the technician's own comment, authored by them, and the audit
record says a human posted it — because a human did. The alternative (a machine
byline on customer-facing text) buys a truthful label at the cost of a machine
that can talk to customers, which is a larger change than this layer is allowed
to make.

### 0.4 One autonomy dial: an **org × persona × risk-class ceiling** that composes with the per-action value by taking the minimum, and can only ever lower

**Decision.** The dial is a new kernel table, `ai_autonomy_ceiling`, with one row
per `(org_id, persona, risk_class)` carrying a level from the ordered vocabulary

```
off  <  suggest  <  require_approval  <  autonomous
```

The first three of those are `approved_action.autonomy`'s existing values
(`AUTONOMY_LEVELS`); `off` is added by this layer and means *this persona may
not even mention this risk class*. Effective autonomy for one proposal is

```
min( ceiling(org, persona, risk_class), approved_action.autonomy )
```

so the dial **can only ever reduce** what the catalogue row already permits. The
catalogue row remains the ceiling on the action; the dial is the ceiling on the
*persona*.

**Rejected: reuse `approved_action.autonomy` alone, with no ceiling.** It is one
column governing four consumers that do not share a threat model — the health
sweep plus three AI personas — and it has no persona axis, so "the copilot may
propose this but the resolver may not" would have to be expressed as one
catalogue row per persona. That is a second registry arriving through the back
door, which is precisely what the kernel registry's docstring says `personas`
exists to prevent.

**Rejected: a single global "AI autonomy" enum per org.** A dial that cannot
distinguish `low` from `high`, or the admin console from an unauthenticated
email-fed resolver, gets set to its most permissive useful value and stays
there.

**Who sets it.** `ai.admin` — already described in `app/core/rbac.py` as high
privilege, admin tier, never technician. Not `health.action.admin`: that grant
governs *what may be run automatically as SYSTEM/root*, which is the catalogue;
this grant governs *which AI persona may reach it*. Both are admin-tier and an
org-admin holds both, so the split costs nothing today and keeps the two
questions separable.

**What is refused at each position.**

| Ceiling | Persona may… |
|---|---|
| `off` | not propose an action of this risk class; the tool is not offered and a request for one is answered "not permitted for this persona" |
| `suggest` | name the action in prose. **No `ai_action` row is created**, so there is nothing to confirm and nothing to dispatch |
| `require_approval` | mint an `ai_action` row, produce a preview, and hold it for a human confirmation (§6) |
| `autonomous` | **unreachable in v1 — see below** |

**What is refused regardless of position.**

1. Anything `is_destructive()` returns `True` for, anything with
   `risk_class = 'high'`, and anything with `max_target_scope <> 'device'` may
   never be `autonomous`. This is not restated here as policy — it is
   `ck_approved_action_autonomous_is_bounded`, a database CHECK, and the dial
   cannot relax a CHECK.
2. Any job type outside `APPROVABLE_JOB_TYPES`. Deny by default, in the kernel,
   for every consumer of the registry.
3. **`autonomous` itself, for every AI persona, in v1.** The ceiling's maximum
   settable value for `persona IN ('end_user','technician','admin')` is
   `require_approval`, enforced by a CHECK on `ai_autonomy_ceiling`, not by
   application code. The health module keeps its `autonomous`; this layer does
   not get it.

**Why the AI personas do not get `autonomous`, when the health sweep does.** The
two triggers are not comparable. A health remediation fires from a **threshold
breach on a signed report from a specific device** — deterministic, deduplicated
by `uq_health_alert_action_alert_kind`, flap-suppressed, and rate-fused with
database ceilings. An AI proposal is **model output**, and model output is
downstream of everything the model has read, including text that arrived through
an unauthenticated mailbox. Reusing the word `autonomous` for both would give
one word two blast radii, which is the exact defect the kernel registry's
`is_destructive` docstring exists to prevent ("two forks of `is_destructive`
means two answers to 'may this run without a human'").

**The perverse incentive accepted, stated plainly.** Every AI act requires a
human click, so the layer's ceiling on usefulness is a human's attention, and
under load humans click through confirmations. This layer's answer is not a
softer dial; it is to make the confirmation *carry information a human can act
on* (§6.3: the preview is rendered from the stored catalogue row, never from the
model's summary of it) and to keep the volume low (§6.5: a per-turn cap on
proposals). If an org wants unattended remediation, the correct product is the
health module's approved-action sweep, which already has it, and which does not
route through a language model.

### 0.5 `ai_action` is the **instance**; `approved_action` is the **catalogue**. It is a new table, and it is the preview

**Decision.** `approved_action` is org policy: *this thing may be run, at this
risk, by these personas, with these fuses*. It is a catalogue and it is long-
lived. `ai_action` is one **proposed occurrence** of a catalogue entry: this
action, on this device, proposed in this turn of this session, with these
rendered params, previewed at this digest, confirmed (or not) by this human at
this time, dispatched as this job.

The relationship is `ai_action.approved_action_id → approved_action.id`, many-to-
one, with the catalogue row's identity **and its `updated_at`** captured into the
preview digest so that editing the catalogue between preview and confirm
invalidates the confirmation (§6.4).

**Rejected: no `ai_action` table — write straight to `jobs` on confirm.** Then
"what did the AI propose that a human refused" is unanswerable, `expires_at` has
nowhere to live, and there is no server-side record that a preview ever happened
— which is exactly the hole §0.6 exists to close.

**Rejected: reuse `health_alert_action`.** It is keyed to an alert
(`UNIQUE (alert_id, kind)`), which is the right idempotency for a threshold
breach and the wrong one for a conversation; an AI proposal has no alert.

**Rejected: put the proposal on `ai_message`.** `ai_message` is on a retention
clock and is hard-deleted (default 30 days). A dispatched action's record must
outlive the transcript that produced it.

### 0.6 A preview is a **server-side resolution**, never a dry run, and the preview row is the proof that it happened — the correction of IPAM's import digest

**Decision.** Producing a preview performs **no part of the act**. It:

- resolves the `approved_action` row for the org (`get_approved_action`);
- resolves the single target device and re-checks it is visible to the proposing
  principal;
- renders the params through `render_params_template` — the same function the
  dispatcher uses, which raises at *write* time on an illegal token, and whose
  `SUBSTITUTABLE_PARAM_KEYS` entries are **all empty today**, so v1 params are
  wholly frozen and contain nothing the model can influence;
- for `script.run`, resolves the pinned `(script_id, script_version)` and its
  exemption, and re-derives `is_destructive` from the pin, never from the row's
  stored boolean alone;
- **reads** the rate fuses (cooldown, per-device-per-day, per-org-per-hour)
  against `jobs` and reports whether the action would currently be fused;
- writes the `ai_action` row, computes `preview_digest`, and returns.

Nothing is signed. Nothing reaches an agent. `JobEngine.create` is not called.
Where a genuine read-only probe already exists for a job type (`patch.scan`
before `patch.apply`), the preview may **cite the most recent existing scan
result** and must not dispatch a fresh one — dispatching a scan to build a
preview is performing an act to describe an act.

**The IPAM lesson, applied.** `app/modules/ipam/importer.py`'s own docstring
records the finding: the import digest "is a consistency check over one
request's inputs, **not proof that a preview ever happened**" — nothing server-
side records that a preview ran, so `apply` can only catch a client echoing a
stale digest, never a caller who skipped preview and computed the digest itself
in one line. IPAM judged that acceptable because `ipam.manage` is admin-tier and
the caller was authorized to write every row anyway.

**That trade does not transfer, and this layer must not repeat the shape.** The
proposing party here is a language model influenced by attacker-controlled text,
and the act is a signed job executing as SYSTEM/root. So:

- **the preview is persisted** (`ai_action`), with `expires_at`;
- **the digest is computed by the server from the stored row**, not from the
  confirm request's body;
- **the dispatcher reads the stored row and nothing else** — the confirm request
  carries an `ai_action_id` and the digest the human was shown, and contributes
  no params, no target, no job type;
- a confirm whose digest does not match the recomputed one is a **409**, not a
  400: the previewed act and the current act are different acts.

There is no path from a request body to a job. That is the property, and §13
tests it by mutation.

### 0.7 The model never names an act's effect — it names a catalogue key, and the catalogue supplies everything else

**Decision.** The only act-related tool any persona is offered is

```
actions.propose(action_key: str, device_id: UUID, rationale: str)
```

`action_key` is resolved against the org's `approved_action` rows filtered by
`persona IN personas`; an unknown or wrong-persona key is a tool error and the
turn continues, exactly as an unknown read tool is today. `device_id` must
resolve to a device the proposing principal can already see. `rationale` is
**stored and displayed but never executed and never audited** (§8.3).

The **entire effect** — job type, params, script pin, risk class, destructiveness,
fuses — comes from the catalogue row. The model contributes a *selection* and
nothing else.

**Rejected: an act tool per job type, with typed params** (`patch.apply(kbs=[…],
reboot=…)`). That is model output reaching `JobEngine.create`'s params, which is
the free-form-expression-from-the-server-side failure that
`SUBSTITUTABLE_PARAM_KEYS`' comment already refuses for operator templates. It
would also mean the AI layer, not the org admin, decides what an action does.

**The cost accepted.** Expressiveness. "Patch the finance group in Friday's
window" is not one catalogue key, and this layer cannot compose it. The AI
persona's act mode in v1 is *"which of the things your admin already approved
should we run on this machine, and why"* — narrower than the scoping brief's
examples, and honestly so. §14 records what a later phase would need.

### 0.8 The replay hole is closed by neutralizing flagged history, not by wrapping it — and act-capable sessions cannot be continued from read sessions

**The finding, verified.** `docs/modules/ai-admin/spec.md` §17.11 and
`app/modules/ai_admin/api.py`'s `_history_messages` docstring record that a
hostile line quoted into an answer is replayed into the **assistant** role on the
next turn, *undelimited*, and is only **flagged** (via the `ai.history` pseudo-
tool) rather than neutralized. The docstring states the reasoning "expires with
read mode." **It has expired.**

**Decision.** For a session whose `mode` is act-capable, the transcript is
replayed as today **except** that any prior assistant row on which `detect()`
fires is replaced by a **server-authored stub** naming the withheld turn and the
records it read:

```
[A previous answer in this conversation quoted content that matched the
 injection heuristic (rule: <rule>, source: <tool>/<source_id>). Its text has
 been withheld from this turn. Ask again if you need it restated.]
```

User rows are never touched — they are the administrator's own words, and
flagging them reports the caller to themselves.

**Rejected: wrap replayed answers in the untrusted envelope.** §17.11's
objection stands and is correct: wrapping a model's own prior answer contradicts
the role it is replayed in and teaches the model the tags are noise, which
degrades the *first*-order defence to buy a weaker second-order one.

**Rejected: summarize prior turns with a second model call.** Cost, latency, and
a second injection surface that produces text nobody reviewed.

**Rejected: leave it flagged, as read mode does.** Read mode's argument was that
a fully successful injection reaches only a read tool the caller already holds.
Act mode's proposal tool is not that, so the premise is gone.

**Stated honestly: `detect()` is a heuristic, so this is depth, not proof.** The
structural bound is §0.7 (the model cannot name an effect) and §6 (a human
confirms a preview rendered from the stored catalogue row). If those two hold, a
laundered instruction's best outcome is *a proposal for something the org admin
already approved, on a device the caller can already see, that a human then
declines*. That is the property this layer defends; the stub is the mitigation
that keeps the same text from being re-offered every turn.

**And: `ai_session.mode` is fixed at creation.** An act-capable session is
created act-capable. There is no promotion, because promoting a session imports
a whole read-mode transcript — assembled under read mode's replay rules — into a
context that can now propose.

### 0.9 The persona that reads the most hostile text has the least reach, and that inversion is the design

**Decision.** Reach is inversely ordered against exposure:

| Persona | Reads text from | May propose an act? |
|---|---|---|
| `end_user` resolver | **unauthenticated ingress** — anyone who can email the intake mailbox | **No. Never. Not at any dial position.** |
| `technician` copilot | ticket threads (semi-trusted), device-originated strings | Yes, bounded to the ticket's linked device |
| `admin` | the whole estate, including ticket bodies | Yes |

**Rejected: give the resolver a small set of "safe" self-service actions**
(restart the print spooler, clear a temp directory). Every one of those is
`script.run`, which `is_destructive` treats as **destructive by default** unless
an exact `(script_id, script_version)` exemption exists — and the requester who
would trigger it holds no permission that would let them run it by hand. The
authority would have to come from somewhere other than the requester, which is
the definition of a privilege side channel. §11.1 also records that the platform
**cannot currently tell which device a requester owns**, so even the target
could not be established.

**The cost accepted.** The persona with the clearest commercial story — "tier-1
tickets that resolve themselves" — is in v1 an answering and deflecting surface,
not a fixing one. That is the honest state of the platform, and §11 says what
would have to exist first.

### 0.10 An act records its **effect**; a question still records only its shape

**Decision.** `docs/modules/ai-admin/spec.md` §0.7's rule is unchanged for
questions: a question is an auditable event, its **text is not written to the
audit log**, because the log is append-only and hash-chained and therefore
un-redactable, is readable by a broader population (`audit.read`) than the
underlying records, and an immutable log of every question every admin ever
asked is a surveillance surface over staff.

An **act** is audited differently, and the difference is not "more text" — it is
**identifiers instead of prose**. `ai.action_proposed`, `ai.action_confirmed`,
`ai.action_dispatched`, `ai.action_rejected` and `ai.action_expired` carry the
catalogue key, the `ai_action` id, the device id, the risk class, the
destructive flag, the effective autonomy, the taint flag, the preview digest,
the confirming user and the resulting `jobs.id` — every one of which is bounded,
non-free-text, and already the kind of identifier the kernel writes for a job.

**What is still not written: the model's `rationale`.** It is model output
influenced by retrieved attacker text; writing it into a permanent chain is the
same defect as writing an `ai_injection_flag` excerpt there, which
`app/modules/ai_admin/models.py` already refuses for exactly this reason ("an
attacker who can write a ticket body could otherwise write arbitrary PERMANENT
content into the tamper-evident chain"). The rationale lives on `ai_action`, on
a retention clock.

**The cost accepted.** An investigation opened after the retention window can
answer *what ran, on what, who authorized it, and which catalogue entry it came
from*, and cannot answer *what the model said to persuade the human*. That is
the same trade §0.7 already took for question text, taken again knowingly.

### 0.11 The confirming human must hold the permission the owning module's own dispatch route requires — which needs a machine-readable map the kernel does not have

**Decision.** Confirming an `ai_action` requires **both**:

1. `ai.act.confirm` (this layer's own grant), **and**
2. the permission that the owning module's dispatch route for that job type is
   gated on — `patch.apply` for `patch.apply`, `deploy.script.run` for
   `script.run`, `deploy.execute` for `app.deploy`, and so on.

Without (2), a technician holding only `ai.act.confirm` could fire `patch.apply`
through this layer while being refused it on `POST /api/v1/patch/apply`. That is
privilege escalation, it violates invariant #6, and it is exactly the governing
principle's negation.

**The kernel gap this exposes.** That mapping exists today only as **English
prose** inside `app/jobs/direct_dispatch.py::MODULE_GOVERNED_REASONS` (each
message names the owning route and its permission) — it is not data, and nothing
can read it. §10.2 specifies the small kernel addition:
`app/jobs/dispatch_permissions.py`, a `job_type → permission code` map with the
same **partition test against the live registry** that
`APPROVABLE_JOB_TYPES`/`NON_APPROVABLE_REASONS` and
`DIRECTLY_DISPATCHABLE_JOB_TYPES`/`MODULE_GOVERNED_REASONS` both already carry —
so a newly registered job type breaks a test instead of picking a side by
accident. That test discipline caught an unclassified job type on its first run
in the health module; it is used here for the same reason.

**Rejected: a `confirm_permission` column on `approved_action`.** Per-action
granularity sounds finer but is worse: it lets an org admin author a row whose
confirm permission is lower than the module's own route requires, which is the
escalation with an extra step and a UI for it.

**Rejected: require `health.remediate`** (the existing high-privilege "dispatch
or approve a remediation" grant). It is health's, its description is about
health's alerts, and reusing it would make this layer's authorization a health
concept.

**A finding about existing code — recorded here, and since CLOSED in health.**
Health's own console path had the shape this decision refuses:
`POST /health/alerts/{id}/remediate` was gated on `health.remediate` alone, so
a holder of that grant could approve a `patch.apply` remediation without
holding `patch.apply`.

That is fixed. `app/modules/health/actions.py::_require_dispatch_grant` now
applies §10.2's map to the resolved `approved_action` row on **both**
dispatching paths — `dispatch_manual_remediation` and `approve_action` — in
addition to the route's own `health.remediate`. `refuse_action` is deliberately
not gated that way: it creates no job, and requiring the higher grant to
*decline* would strand approval requests that nobody may clear. See
`docs/modules/endpoint-health-alerting.md` §20 for health's own account, and
`backend/tests/test_health_remediation_authz.py` for the mutations that pin it
— including the one that matters most here, `dispatch_permission_for`
returning `None` being read as a pass, which dispatched an `agent.cert.renew`
job from the alert console.

Kept rather than deleted because the reasoning above is why §10.2's map exists
at all, and because the map's docstring now names health as a consumer: this
layer did not inherit health's behaviour, and health no longer has it.

### 0.12 Fleet-wide is structurally impossible here for the same reason it is in health: one row, one device

**Decision.** `ai_action.target_device_id` is a single, `NOT NULL` `UUID`. There
is no group column, no target array, no selector union. A "campaign" is N rows,
each with its own preview, its own digest, and its own confirmation.

This mirrors `health.actions._dispatch_remediation`, whose docstring makes the
same claim about its own signature — and mirrors the **correction** that
followed it: the kernel registry now records that "one job, one device" bounds
*one dispatch* and says nothing about *how many*, which is why
`MAX_PER_ORG_PER_HOUR` and `MAX_PER_DEVICE_PER_DAY` acquired ceilings. This layer
therefore adds its own bound at the other end: **a per-turn cap on how many
`ai_action` rows one turn may mint** (§6.5), and the catalogue row's existing
fuses are evaluated at preview *and* re-evaluated at dispatch.

**Rejected: a target-set preview** ("this runs on 37 devices in group Finance —
confirm"), which the scoping brief asks for. It requires widening
`max_target_scope` beyond `('device',)` — a reviewed kernel migration the
registry's `MAX_TARGET_SCOPES` comment anticipates — and it means one human
click authorizes 37 signed jobs chosen by a model. §14 defers it; it is not v1.

### 0.13 Where the scoping material and the code disagreed

Recorded here because the code wins and a reader should not have to re-derive
the discrepancies.

1. **`APPROVABLE_JOB_TYPES` lives in `app/core/approved_actions.py`, not in
   `app/modules/health/approved_actions.py`.** The health file is the *service
   layer* (CRUD, validation, audit) that consumes it; the kernel file holds the
   closed maps, `is_destructive`, `render_params_template` and the org-scoped
   reads. The two stated conditions, the deny-by-default posture and the
   partition test (`tests/test_approved_actions_destructive.py`) are all as
   briefed — only the location differed.
2. **There is no autonomy *dial* today, in any module.** `approved_action.
   autonomy` is a **per-row** column (`AUTONOMY_LEVELS = ('suggest',
   'require_approval', 'autonomous')`). `health.action.admin`'s permission
   description says "set the autonomy dial", which is that column. A dial
   spanning personas and risk classes does not exist and is new kernel state
   (§10.1) — the brief's phrasing implies it is already there.
3. **`ai_action` is not merely uncreated — nothing anywhere references it.**
   It is named in `docs/modules/ai-admin/spec.md` §12.2 and in
   `docs/modules/ai-admin.md`, and nowhere in code.
4. **IPAM's import digest is not "a client-side control only".** It is
   recomputed **server-side** — over the file *and* the mapping — but over the
   inputs of the *same request*, so it cannot prove a preview ever ran. The
   distinction matters because the fix is different: not "move the check to the
   server" (it is there) but "persist the preview" (§0.6).
5. **`docs/modules/ai-admin/spec.md` §12.3's claim that "no kernel change is
   required for the seam" is true of the *seam* and false of the *layer*.** The
   seam attaches to `approved_action` unchanged. The layer needs three kernel
   changes (§10), none of which touches `approved_action`'s columns.

---

## 1. Purpose and boundary

**In scope.** The shared orchestration service; the two new personas; the
autonomy dial; the `ai_action` instance table and its preview→confirm→execute
lifecycle; the act-mode injection defences; the `ai.*` audit extension; the RBAC
and tenancy rules that make all of it safe.

**Out of scope, permanently.**

- Any new agent capability, job type, or signed-job path. Every act is an
  existing job type from an existing catalogue row.
- Any new *power*. If no human on this platform can do a thing, this layer
  cannot either.
- Modifying `approved_action`'s columns or its closed maps. This layer *reads*
  the kernel registry and *adds persona strings to existing rows*; it does not
  fork it, extend it, or add a parallel one.
- A second model-provider integration path. `app/core/ai/providers/` is the only
  egress, and the two-construction-sites assertion moves with it (§0.1).
- Autonomous (unconfirmed) AI action (§0.4).

**Out of scope for v1, deferred with a reason** — §14.

---

## 2. The orchestration service

### 2.1 What is shared (kernel, `app/core/ai/`)

| Concern | Today | After |
|---|---|---|
| Turn loop, budgets, stop reasons | `ai_admin/orchestrator.py` | `core/ai/orchestrator.py` |
| Provider clients (`httpx` only) | `ai_admin/providers/` | `core/ai/providers/` |
| Untrusted envelope + nonce-leak abort | `ai_admin/envelope.py` | `core/ai/envelope.py` |
| Fail-closed response scrubber | `ai_admin/scrub.py` | `core/ai/scrub.py` |
| Injection heuristic | `ai_admin/detect.py` | `core/ai/detect.py` |
| Staff loopback executor | `ai_admin/executor.py` | `core/ai/executor_staff.py` |
| **Portal loopback executor** | — | `core/ai/executor_portal.py` (new, §3.1) |
| Transcript store, audit writer, retention | `ai_admin/store.py`, `retention.py` | `core/ai/store.py`, `core/ai/retention.py` |
| Org settings + kill switch | `ai_admin/settings.py` | `core/ai/settings.py` |
| **Act: propose, preview, confirm, dispatch** | — | `core/ai/act.py` (new) |
| **The dial** | — | `core/ai/autonomy.py` (new) |
| **Read-tool allowlist + arg models** | `ai_admin/tools/` | `core/ai/tools/` (§19.16) |

### 2.2 What is per-persona

A `PersonaProfile` is a frozen dataclass in the persona's own module,
registered into a `MappingProxyType` at import time — the same immutability
posture `ai_admin/tools/registry.py` already uses, for the same reason (whatever
compromises the database must not choose the agent's capabilities).

```python
@dataclass(frozen=True)
class PersonaProfile:
    name: Literal["admin", "technician", "end_user"]
    system_prompt: str                      # a constant; never assembled from data
    executor: Literal["staff", "portal"]
    tools: Mapping[str, ReadTool]           # frozen, per-persona
    may_propose: bool                       # end_user: False, always (§0.9)
    replay_policy: Literal["verbatim", "neutralize_flagged"]
```

`system_prompt` is a module-level constant, never org-editable — `prompt.py`'s
existing docstring gives the reason and it holds for all three personas: an
org-editable prompt makes the system prompt *data*, which is the hole a constant
prompt closes.

### 2.3 The boundary, stated as a test

Three properties, each a test rather than a convention:

1. `app/core/ai/` imports no `app.modules.*` package.
2. No persona module constructs an HTTP client, reads a provider key, or writes
   an `ai.*` audit event. (The existing two-construction-sites assertion,
   relocated and widened per §0.1.)
3. Every registered `PersonaProfile` has `may_propose is False` **or** appears in
   `ai_autonomy_ceiling`'s CHECK-constrained persona list; a persona that is
   neither is a build failure, not a runtime default.

---

## 3. The three personas

### 3.1 `end_user` — the tier-1 resolver

**Principal.** A `PortalContext` from `resolve_portal_principal`. Reads run in
`portal_session` with both GUCs pinned; if either is unset no rows match, which
is the portal's existing fail-closed property and this persona inherits it
rather than re-deriving it.

**May read.** Only through `core/ai/executor_portal.py`, and only the portal's
own routes:

- the requester's own ticket list and one ticket's detail;
- that ticket's **public** thread (never internal notes — the portal's own
  routes already exclude them, and this persona gets no route the portal UI
  does not have);
- the org's ticket categories, as the portal exposes them.

**May propose.** Nothing that dispatches, ever (§0.9). It may return:

- a prose answer built from the above;
- a suggested **category** and **subject** for a ticket the requester is about to
  file;
- a suggested public comment for the requester to send **on their own ticket**,
  returned to their editor, never posted.

**May never.** Touch the staff executor; read any device, alert, job, inventory,
patch, credential or audit row; read another requester's ticket (structurally
impossible — RLS, not application code); read an internal note; propose an
`ai_action`; be raised above `off` in `ai_autonomy_ceiling` for any risk class.

**Ingress hostility, named.** The text this persona reads includes ticket bodies
created by `email_intake` from messages **anyone who can reach the intake
mailbox** can send. `docs/modules/ai-admin/spec.md` §8.5 already traces that
attack end to end through the real ingress, and
`tests/test_ai_admin_boundaries.py` borrows the email-intake suite's own fixture
so the hostile text arrives through the genuine path. This layer's act-mode
tests do the same (§13).

### 3.2 `technician` — the copilot

**Principal.** The technician's own `AuthContext`, through the existing staff
loopback executor with their own bearer token. `require_permission` and RLS run
verbatim; the copilot has no power the technician does not.

**May read.** The persona's tool set, which is the admin tool set intersected
with what the technician's grants actually permit — plus, in ticket context, the
ticket, its full thread including internal notes (the technician holds
`ticket.read`), and the linked device's state where `ticket.device_id` is set
and `devices.read` is held.

**May propose.** An `ai_action` **bounded to `ticket.device_id`**. If the ticket
has no linked device, the copilot cannot propose — it can only say so. This is
deliberate: the ticket's device link is the one thing tying the conversation to
a machine, and letting the model choose an arbitrary device would make "the
ticket" decorative.

**May never.** Post a comment, set an assignment, change a status, change a
priority, resolve or close (§0.3). Propose an action whose catalogue row does not
carry `'technician'` in `personas`. Propose against a device the technician
cannot see (the proposal tool validates through the same loopback read, so this
is enforced by `require_permission`, not by a filter).

### 3.3 `admin` — the platform-ops copilot (Phase 17, act half)

Unchanged from `docs/modules/ai-admin/spec.md` in read mode. In act mode it gains
the `actions.propose` tool over rows carrying `'admin'` in `personas`, and its
`ai_session.mode` may be created act-capable.

This is the **only** change §12 of that spec needs, and it is additive: `ActTool`
implements the same `Tool` protocol, `ToolResult.mutating` becomes reachable as
`True` for the first time, and the audit writer and UI already branch on it.

---

## 4. The autonomy dial

### 4.1 `ai_autonomy_ceiling` (new kernel table)

| Column | Type | Notes |
|---|---|---|
| `org_id` | uuid NOT NULL FK `organizations.id` | PK part 1 |
| `persona` | varchar(16) NOT NULL | PK part 2, CHECK `IN ('end_user','technician','admin')` |
| `risk_class` | varchar(8) NOT NULL | PK part 3, CHECK `IN ('low','medium','high')` |
| `level` | varchar(20) NOT NULL | CHECK `IN ('off','suggest','require_approval')` |
| `updated_by` | uuid NULL FK `users.id` | |
| `updated_at` | timestamptz NOT NULL | |

Primary key `(org_id, persona, risk_class)`. RLS on `org_id`, and every read
carries an explicit `org_id` predicate in addition to RLS — the kernel
registry's own reads do this deliberately ("RLS is the backstop, not the only
filter"), because a query leaning on RLS alone returns the wrong row on the
admin engine.

**`level` has no `'autonomous'` value at all.** §0.4: the vocabulary is ordered
and the AI personas' vocabulary simply stops one short. Making that a CHECK
rather than application logic means widening it is a migration a reviewer sees —
the same argument `ck_approved_action_autonomous_is_bounded` makes for its own
gate.

**A missing row means `off`.** Deny by default, consistent with every other
allowlist in this codebase. The layer ships with no rows, so it ships inert.

*(The comparison this sentence used to draw — "the way `ai_settings.enabled`
defaults `false`" — no longer holds: that column was dropped on 2026-08-25, see
`docs/modules/ai-admin/spec.md` §0.14. **Nothing about the autonomy ceilings
changes.** The assistant being available is not the same decision as an
automated actor being allowed to act, and this layer's deny-by-default is
untouched: an org that has configured a provider still has zero ceiling rows
until somebody writes one.)*

### 4.2 Resolution

```
effective(persona, action) =
    min_level( ceiling(org, persona, action.risk_class) ,
               action.autonomy ,
               'require_approval' )          # the v1 clamp, §0.4
```

evaluated **twice**: at proposal (so an `off` persona never mints a row) and
again at confirm (so lowering the dial between preview and confirm refuses the
dispatch). The second evaluation is not redundant — the dial is org state that
an admin may change at any moment, and a confirmation is a decision about *now*.

If `action.personas` does not contain the persona, the answer is `off` before
the dial is consulted at all.

---

## 5. Data model

### 5.1 `ai_action` (new)

One proposed occurrence of a catalogue entry.

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK (uuid7) | |
| `org_id` | uuid NOT NULL FK `organizations.id` | RLS + explicit predicate |
| `session_id` | uuid NOT NULL FK `ai_session.id` ON DELETE **RESTRICT** | see below |
| `message_seq` | int NOT NULL | the turn that proposed it |
| `persona` | varchar(16) NOT NULL | CHECK as §4.1 |
| `approved_action_id` | uuid NOT NULL FK `approved_action.id` | the **catalogue** entry |
| `approved_action_key` | varchar(100) NOT NULL | denormalized, so the record survives a catalogue delete |
| `job_type` | varchar(100) NOT NULL | denormalized for the same reason |
| `target_device_id` | uuid NOT NULL FK `devices.id` | **single device**, §0.12 |
| `rendered_params` | jsonb NOT NULL | from `render_params_template`, never from a request |
| `script_id` / `script_version` | uuid NULL / int NULL | the pin, when `job_type='script.run'` |
| `risk_class` | varchar(8) NOT NULL | snapshot at preview |
| `destructive` | bool NOT NULL | re-derived by `is_destructive`, never accepted |
| `effective_autonomy` | varchar(20) NOT NULL | §4.2's result at preview |
| `tainted` | bool NOT NULL DEFAULT false | §7.3 |
| `preview_digest` | char(64) NOT NULL | §6.4 |
| `rationale` | text NOT NULL DEFAULT '' | model prose. On the transcript clock (§8.3) |
| `state` | varchar(16) NOT NULL | §5.2 |
| `expires_at` | timestamptz NOT NULL | |
| `confirmed_by` / `confirmed_at` | uuid NULL FK `users.id` / timestamptz NULL | |
| `rejected_reason` | varchar(200) NULL | |
| `job_id` | uuid NULL FK `jobs.id` | set once, on dispatch |
| `created_at` / `updated_at` | timestamptz NOT NULL | |

Indexes: `(org_id, state, expires_at)` for the expiry sweep; `(org_id,
session_id)` for the session view; `(org_id, target_device_id)` for "what has
the AI done to this machine".

**`ON DELETE RESTRICT` on `session_id`, deliberately.** `ai_message` cascades
from `ai_session` because a transcript is deletable *because it is not audit*.
An `ai_action` that reached `dispatched` is not: it names a signed job that ran
on somebody's machine. The retention sweep therefore **blanks `rationale` and
nothing else** on this table (§8.3), and a session with a dispatched action
cannot be hard-deleted out from under it.

**`params_template` is not copied here.** `rendered_params` is the rendered
result; the template lives on the catalogue row where `redact_secret_values`
already governs its read path.

### 5.2 Lifecycle

```
                 ┌──────────── rejected (human declined)
                 │
proposed ──▶ previewed ──┼──────────── expired  (expires_at passed, sweep)
                 │
                 └── confirmed ──▶ dispatched ──▶ settled
                                        │
                                        └──▶ failed
```

- **`proposed`** — the tool call was accepted; the row exists. Transient: the
  preview is computed in the same transaction, so a row observed in `proposed`
  means a crash between insert and digest, and the sweep expires it.
- **`previewed`** — digest computed, awaiting a human. The only state a confirm
  may act on.
- **`confirmed`** — a human with the right grants (§9.2) matched the digest.
  Transient: dispatch follows in the same transaction.
- **`dispatched`** — `job_id` set. Terminal for this layer; the job's own
  lifecycle is the kernel's.
- **`settled`** — the job reached a terminal state; set by the same result path
  that already updates `jobs`, so this layer runs no poller.
- **`rejected` / `expired` / `failed`** — terminal.

Transitions are one-way and enforced by a CHECK plus a guarded update; a
confirmed row can never return to `previewed` and be re-confirmed.

### 5.3 Changes to existing tables

- `ai_session.mode` — CHECK widened from `IN ('read')` to `IN ('read','act')` by
  a reviewed migration, exactly as `docs/modules/ai-admin/spec.md` §12.2
  anticipates. Fixed at creation (§0.8).
- `ai_session.persona` — CHECK widened from `IN ('admin')` to
  `IN ('admin','technician','end_user')`.
- `approved_action.personas` — **no schema change.** Existing rows gain
  `'admin'`, `'technician'` and/or `'end_user'` as *data*, through the health
  module's existing `approved-actions` write path, which already validates and
  audits. The column shipped for this (`["health"]` in v1, "precisely so Phase
  17 adds values to EXISTING ROWS rather than a second table nobody
  reconciles"). No new registry, no new column, no fork.

**Nothing else changes.** `approved_action`'s closed maps, `is_destructive`,
`render_params_template`, the fuse ceilings and every CHECK are consumed
unmodified.

---

## 6. Preview → confirm → execute

### 6.1 Propose

The model calls `actions.propose(action_key, device_id, rationale)`. The server:

1. resolves `action_key` via `get_approved_action(session, org_id=…, key=…)`;
   unknown → tool error, turn continues;
2. refuses if `persona NOT IN action.personas`, or `action.active is False`;
3. refuses if `non_approvable_reason(action.job_type)` is not `None` — deny by
   default at the kernel, re-checked here because the row may predate the
   allowlist (the kernel comment anticipates exactly this: "a re-derivation over
   rows written before the allowlist existed");
4. resolves the effective autonomy (§4.2); `off`/`suggest` → no row is created,
   and the model is told which, truthfully;
5. verifies the target device is visible to the proposing principal **through a
   loopback read**, not through a query — so `require_permission` and RLS decide,
   not this layer;
6. for `technician`, verifies `device_id == ticket.device_id`;
7. writes the `ai_action` row and produces the preview (§6.2).

### 6.2 What a preview contains

Rendered **entirely from the stored catalogue row and the resolved target** —
never from the model's prose:

- the catalogue entry's own `name`, `description`, `key`, `job_type`;
- `rendered_params`, with `redact_secret_values` applied on the read path (the
  registry's own read-side redaction, reused, because a legacy row may carry a
  credential-named key even though the write gate now refuses one);
- for `script.run`: the script **name and version**, never the body — health's
  approval path already states the rule ("nobody approves a script they cannot
  identify"; the body is arbitrary code, which is why `script.run` is
  destructive by default);
- the single target device's hostname and id;
- `risk_class`, `destructive`, `effective_autonomy`, `tainted`;
- the **fuse verdict**: whether cooldown, per-device-per-day or per-org-per-hour
  would currently block this dispatch, and when it next would not;
- the model's `rationale`, rendered in a visually distinct, clearly-labelled
  region as *the assistant's argument*, never as part of the action description.

No job is signed. No agent is contacted. Nothing is written outside `ai_action`.

### 6.3 What the human sees, and why the layout is a security property

The catalogue-derived facts are the answer to *what will happen*. The model's
rationale is the answer to *why it was suggested*. A UI that lets the second
region describe the first re-introduces the whole injection problem at the
confirmation step — a laundered instruction that renders as an authoritative
action description is a confirmation the human did not really give. The
separation is therefore specified here, not left to the frontend, and §13 tests
that the confirm payload's action fields come from the stored row.

### 6.4 The digest, and what binds a confirmation to a previewed act

```
preview_digest = sha256( canonical_json({
    "ai_action_id":            <uuid>,
    "approved_action_id":      <uuid>,
    "approved_action_updated_at": <iso8601>,   # a catalogue edit invalidates it
    "job_type":                <str>,
    "rendered_params":         <canonicalized jsonb>,
    "script_id":               <uuid|null>,
    "script_version":          <int|null>,
    "target_device_id":        <uuid>,
    "risk_class":              <str>,
    "destructive":             <bool>,
    "effective_autonomy":      <str>,
}) )
```

**Computed by the server from the stored row, at preview and again at confirm.**
The confirm request carries `ai_action_id` and the digest the human was shown;
it carries **no** job type, no params, no target.

**The stored `preview_digest` is the authority, and BOTH comparisons are against
it** (amended by §19.18, which found the code comparing only against the value
the caller sent):

1. `recompute(stored row, catalogue row) == ai_action.preview_digest` — *the
   world has not moved since the server issued this preview*. The caller
   contributes nothing to this comparison, which is what makes it a server-side
   invariant rather than a client-side one.
2. `request.digest == ai_action.preview_digest` — *this confirmation is of the
   preview that was issued*. "The digest without the row confirms an act nobody
   previewed."

- mismatch on either → **409**, with a message naming what changed (catalogue
  edited, dial lowered, script version bumped). Not a 400: the request is
  well-formed; the world moved. When both are true the FIRST is reported: an
  operator can act on "the catalogue was edited"; "your client sent the wrong
  digest" is bookkeeping.
- `state <> 'previewed'` → **409**.
- `now > expires_at` → **410**, and the sweep marks it `expired`.

**This is the property IPAM's importer does not have and says so.** The proof
that a preview happened is *the persisted preview row*; the digest's job is only
to catch the row having changed underneath the human. Both halves are needed:
the row without the digest confirms a stale act, the digest without the row
confirms an act nobody previewed.

**Expiry.** `expires_at` defaults to 15 minutes, org-configurable within a
CHECK-bounded range (60 s – 24 h). An unbounded expiry is a confirmation token
that outlives the situation that justified it; health's `approval_ttl_s` is
bounded `300…604800` for the same reason.

### 6.5 Execute

On a valid confirm, in **one transaction**:

1. re-resolve the catalogue row and re-derive `is_destructive` from the pin;
2. re-evaluate the dial (§4.2);
3. re-evaluate the rate fuses against `jobs` — the read at preview was
   informational, this one is the gate;
4. re-check the confirming principal's permissions (§9.2);
5. `JobEngine.create(session, org_id=…, type=action.job_type,
   target_device_id=ai_action.target_device_id, params=ai_action.rendered_params,
   requested_by=<confirming user>)` — **the unmodified engine**, with
   `requested_by` set to the human, because a human authorized it;
6. set `state='dispatched'`, `job_id`;
7. write `ai.action_dispatched` (§8.2), in addition to the kernel's own job
   record — the kernel record says a job was sent, this one says *why an AI
   proposed it and who agreed*.

**Per-turn cap.** A single turn may mint at most **3** `ai_action` rows
(org-configurable, CHECK-bounded 1–10). Beyond it the tool returns an error the
model can act on and the turn continues. This is the bound §0.12 adds at the
other end from `max_target_scope`: one device per row is a claim about one
dispatch, and the cap is the claim about how many.

---

## 7. Prompt injection, act-mode edition

Read mode's defence is **capability bounding**: retrieved text cannot invoke
anything outside a frozen GET-only allowlist, so a fully successful injection
reaches a read tool the caller already holds and nothing else. Act mode removes
that premise. Four structural replacements, in order of load-bearingness.

### 7.1 The effect is not in the model's output (§0.7)

The model emits a catalogue **key** and a device id. Job type, params, script
pin and risk all come from a row an org admin authored through an audited,
validated write path. Model output cannot construct an effect; it can only
select among effects a human already approved. This is the replacement for the
frozen GET allowlist and it is the one that matters.

`SUBSTITUTABLE_PARAM_KEYS` is empty for every job type today, so v1 params are
not merely constrained — they are constants. If a future entry becomes non-empty,
the substituted values are still `{device_id}` and `{selector}` only, both
server-resolved.

### 7.2 A human confirms a server-rendered preview (§6.3)

The confirmation screen's action description is derived from the stored row. The
model's rationale is separately regioned and labelled. A successful injection's
best case is a *proposal* the human then reads and declines.

### 7.3 Taint tracking

An `ai_action` minted in a turn where `detect()` fired on **any** retrieved
content, or where any read touched a record whose provenance is unauthenticated
ingress (`ticket.origin='email'`, and any comment created by `email_intake`), is
written with `tainted = true`.

Taint does three things: it is displayed prominently on the confirmation screen,
it is carried into `ai.action_proposed`'s metadata, and it **caps
`effective_autonomy` at `require_approval`** regardless of the dial. In v1 the
cap is already the global clamp, so taint's real work today is *visibility* —
and that is stated rather than overclaimed, because a control whose effect is
currently a no-op will be deleted by someone tidying up unless the reason it
exists is written down.

### 7.4 Replayed history is neutralized, not merely flagged (§0.8)

For act-capable sessions, any prior **assistant** row on which `detect()` fires
is replaced by a server-authored stub. User rows are untouched.
`api._replayed_findings`' existing flagging via the `ai.history` pseudo-tool
continues unchanged for both modes — the stub is *in addition to* the flag, not
instead of it: the flag is the record that it happened, the stub is the
neutralization.

`docs/modules/ai-admin/spec.md` §17.11's closing sentence — "this reasoning
expires with read mode" — is discharged by this section, and that spec should be
amended to point here.

### 7.5 What is unchanged, and must stay unchanged

The envelope with its per-turn nonce and nonce-leak abort; the constant system
prompt; the fail-closed scrubber with its declared `response_fields` contract;
`ReadTool.method: Literal["GET"]`; the loopback executor carrying the caller's
own token; `_NEVER_A_TOOL` and `_FORBIDDEN_GATES`. `envelope.py`'s header states
that a later task may not remove, widen or make configurable any of them and
that a brief appearing to require it is wrong. **That includes this brief.**
Act mode adds a tool type; it weakens no gate.

---

## 8. Audit

One AI audit story, in the existing `ai.*` namespace, through
`app/core/audit.py::audit_service.record`.

### 8.1 Unchanged from read mode

`ai.session_started`, `ai.query`, `ai.injection_flagged`, `ai.settings_changed`
keep their shapes. `ai.session_started` gains `persona` values beyond `'admin'`
and `mode='act'`. `ai.query`'s rule is unchanged: **the question's hash and
length, never its text** (§0.10).

### 8.2 New events

| Action | When | Metadata |
|---|---|---|
| `ai.action_proposed` | an `ai_action` row reaches `previewed` | `ai_action_id`, `session_id`, `persona`, `approved_action_id`, `approved_action_key`, `job_type`, `target_device_id`, `risk_class`, `destructive`, `effective_autonomy`, `tainted`, `preview_digest`, `ticket_id` (§19.20 — ticket-bound personas only, and ABSENT rather than null otherwise) |
| `ai.action_confirmed` | a human confirms | `ai_action_id`, `confirmed_by`, `preview_digest`, `seconds_since_preview` |
| `ai.action_rejected` | a human declines | `ai_action_id`, `rejected_by`, `rejected_reason` (a bounded enum, not free text) |
| `ai.action_expired` | the sweep expires a row | `ai_action_id`, `age_s` |
| `ai.action_dispatched` | the job is created | `ai_action_id`, `job_id`, `approved_action_key`, `job_type`, `target_device_id`, `confirmed_by`, `tainted` |
| `ai.autonomy_changed` | `ai_autonomy_ceiling` write | `persona`, `risk_class`, old level, new level |

`actor_type='user'` throughout, `actor_id` = the proposing or confirming human.
There is no `actor_type='ai'`: the platform's actor vocabulary is
`user | agent | system`, and inventing a fourth would make every existing audit
consumer's exhaustiveness check wrong. The *fact* that an AI proposed it is
`ai.action_proposed` existing at all.

`ai.action_dispatched` sits **beside** the kernel's own job record rather than
replacing it — the same relationship health's `health.remediation_dispatched`
has, and for the same stated reason.

### 8.3 What an act records, and why it differs from a question

A question's audit record is deliberately shape-only because the *text* is the
sensitive thing and the log is permanent, broadly readable, and un-redactable
(§0.10). An act's audit record is **complete**, because the sensitive thing
about an act is not prose — it is *which signed job ran on whose machine on
whose authority*, and every field of that is a bounded identifier the kernel
already writes elsewhere.

So the rule is not "acts log more." It is: **acts log identifiers; questions log
hashes; neither logs prose.**

**Explicitly not written to audit:** `ai_action.rationale`. It is model output
downstream of retrieved text, and the audit chain is where an attacker who can
write a ticket body would most like their text to end up permanently. It lives
on `ai_action` and the retention sweep **blanks it on the transcript's clock**,
while every other column on the row survives. `ai_message`'s own header already
makes this argument for `ai_injection_flag` excerpts; this is the same argument
about the same threat.

**Every copy of that text owes the same clock** — `ai-admin` §0.7's rule,
inherited. `rationale` is a denormalization of model output onto a durable row,
so it carries the obligation.

---

## 9. Tenancy and RBAC

### 9.1 Tenancy (invariant #5)

- `ai_action` and `ai_autonomy_ceiling` are `org_id NOT NULL`, RLS-protected,
  with **an explicit `org_id` predicate on every query in addition to RLS** —
  the kernel registry's own reads do this and its comment says why (a query
  leaning on RLS alone returns the wrong row the moment it runs on the admin
  engine: a worker sweep, a migration fixup, a test).
- No global rows. A cross-tenant AI action would be a cross-tenant command path,
  which is the reason `approved_action` has no global rows either.
- `ai_session.org_id` is pinned at creation and a mismatch is a 409, never a
  switch — `ai-admin` §0.9, inherited unchanged.
- The `end_user` persona additionally runs under `portal_session`'s two GUCs, so
  its isolation is row-level *within* an org and is enforced by the database.
- The expiry sweep runs per-org, and a failure in one org must not abort the
  others — the pattern the worker already had to be corrected into.

### 9.2 RBAC (invariant #6)

Two new permissions, and neither grants anything on its own — the same posture
`ai.use` already takes:

| Code | Tier | Grants |
|---|---|---|
| `ai.act.propose` | technician | May open an act-capable session and mint proposals. Grants **no** ability to confirm or dispatch. |
| `ai.act.confirm` | admin | May confirm a previewed action. Grants **nothing** by itself — see below. |

**The confirm check is two-part and the second part is the load-bearing one**
(§0.11):

```
require ai.act.confirm
AND     require dispatch_permission_for(job_type)   # §10.2
AND     the confirming principal can read the target device (loopback GET)
```

So a technician who cannot run `patch.apply` by hand cannot confirm one here,
and `ai.act.confirm` is not a skeleton key. `ai.admin` (existing) additionally
governs `ai_autonomy_ceiling`.

The `end_user` persona needs no new permission: `portal.ticket.read` is its
entire entitlement, and it proposes nothing.

Route-derived RBAC completeness is asserted from the router the way every other
module does it (`app.core.rbac.route_permission`), including the portal's
`require_portal_permission`, which was built to be recoverable by the same
reflection.

---

## 10. Kernel changes this layer requires

Three, all small, all reviewable. `docs/modules/ai-admin/spec.md` §12.3 is
correct that the **seam** needs none; the **layer** needs these.

### 10.1 `ai_autonomy_ceiling`

New kernel table (§4.1), in `app/core/ai/autonomy.py` with its declarative model
beside the other kernel models in `app/models/` — the same forced split
`app/core/approved_actions.py` documents (a declarative model under `app/core/`
cannot be registered in `app/models/__init__.py` without a verified import
cycle).

### 10.2 `app/jobs/dispatch_permissions.py`

A machine-readable `job_type → permission code` map: *which permission the
owning module's own dispatch route requires*. Today this exists only as English
inside `direct_dispatch.MODULE_GOVERNED_REASONS`.

It ships with the **partition test** both existing allowlists carry: every name
in `app.jobs.registry.registered_names()` is either in the map or in an
explicit `NO_HUMAN_DISPATCH_PATH` set with a stated reason, and the two are
disjoint. A newly registered job type breaks a test rather than defaulting to
either answer — the discipline that caught an unclassified job type on its first
run in the health module.

This is useful beyond this layer: it is the data behind
`MODULE_GOVERNED_REASONS`' prose, and it is what would let health's own
`/alerts/{id}/remediate` close the gap §0.11 records.

### 10.3 `ai_session.mode` and `ai_session.persona` CHECK widening

Anticipated by `docs/modules/ai-admin/spec.md` §12.2 and by
`app/modules/ai_admin/models.py`' docstring ("a widening you have to write a
migration for is a widening someone reads"). One migration, two `ALTER`s.

**Not required, explicitly:** any change to `approved_action`'s columns, closed
maps, CHECKs or fuses; any change to `JobEngine`; any change to the agent.

---

## 11. Blocked on something that does not exist

Specifying these as if buildable would be the failure this document was written
to prevent. Each is stated as a **precondition**, not a feature.

### 11.1 Device ownership — blocks *any* end-user-initiated act, permanently, until it exists

**`devices` has no owner, primary-user, or assigned-to column.** Verified
against `app/models/core.py`: the table carries hostname, fqdn, os, serial,
asset tag, cpu, ram, disks, nics, group, status and timestamps, and nothing
linking a device to a person. `device_tags` is a free key/value pair with no
semantics.

The only device↔person link on the platform is `ticket.device_id`, which is
**nullable and asserted by whoever created the ticket** — including, for an
email-created ticket, by a parser reading text an unauthenticated sender wrote.

Consequently: "run the safe fix on the requester's laptop" has no server-side
truth to resolve. Even if §0.9 were reversed, the target could not be
established, and resolving it from a ticket field would let an unauthenticated
email choose which machine a job runs on. **Device ownership is a kernel/
inventory concern and must be built there** — an authenticated, auditable
assignment — before any end-user-initiated act is designed.

### 11.2 A published knowledge base — blocks the resolver's actual value

**`doc_articles` has no visibility, audience or published flag**, and every
documentation route is gated on `docs.read`, a staff permission a portal user
does not hold. The IT documentation module is an *internal* runbook and records
store (network, service, vendor, location records) — precisely the corpus that
must not be readable by a requester.

So the tier-1 resolver has **no end-user-facing corpus to answer from**. It can
answer questions about the requester's own tickets and it can route; it cannot
say "here is how to reconnect the VPN," because no article on this platform is
marked as safe to show a requester.

The precondition is a documentation-module change: an audience/visibility
dimension on articles plus a portal-facing read route. That is that module's
work, not this layer's, and this layer must not invent a second article store to
route around it.

**Resolved 2026-09-23 (operator-approved).** `doc_articles.portal_visible`
(migration 0160, default off) marks an article as published to the portal.
Requesters read that slice through `GET /portal/help/suggest` and
`GET /portal/help/articles/{id}`, gated `portal.ticket.read`; a restrictive
`portal_isolation` policy and a column-level grant confine `portal_user` to
flagged, unarchived articles and to `id`/`title`/`body`. The resolver gained
two tools on those routes (`portal.help_suggest`, `portal.help_article`). No
`/docs` route was widened; the tool-set test still fails on any `/docs` path.

### 11.3 A ticket-deflection surface

The portal has no "search before you file" step (`app/modules/portal/api.py`
registers category list, ticket list, ticket detail, create, comment, and
attachment routes — no search). The resolver's highest-value placement is
pre-filing deflection, and it has nowhere to stand until the portal has that
step. Not blocking for the resolver's in-ticket answering; blocking for the
commercial claim.

### 11.4 A reporting/analytics query layer

The scoping brief pairs read mode with "Reporting & Analytics" and asks act mode
to "trigger reports". No reporting module exists. `actions.propose` cannot name
a report because there is no job type and no catalogue entry for one.

### 11.5 Slack/Teams surfaces

Named in the brief as later. They are a **new unauthenticated-adjacent ingress**
and a new egress, and each needs its own decision about how a principal is
established. Not v1, and not a UI detail.

---

## 12. API surface

Route tables are indicative; the definitive list is the router, and RBAC
completeness is derived from it.

**Act (staff, `/api/v1/ai`)**

| Method | Path | Permission |
|---|---|---|
| `POST` | `/ai/sessions` (gains `mode`, `persona`) | `ai.use` (+ `ai.act.propose` for `mode='act'`) |
| `GET` | `/ai/actions` | `ai.use` + `devices.read` (§19.19) |
| `GET` | `/ai/actions/{ai_action_id}` | `ai.use` + `devices.read` (§19.19) |
| `POST` | `/ai/actions/{ai_action_id}/confirm` | `ai.act.confirm` + §9.2's second part |
| `POST` | `/ai/actions/{ai_action_id}/reject` | `ai.act.propose` |
| `GET` / `PUT` | `/ai/autonomy` | `ai.admin` |

**Copilot (`/api/v1/service-desk/tickets/{ticket_id}/ai`)** — session create and
turn, gated on `ticket.read` plus `ai.use`; the persona profile does the rest.

**Resolver (`/api/v1/portal/ai`)** — session create and turn, gated by
`require_portal_permission("portal.ticket.read")`. No action routes exist on
this surface **at all**; their absence is a test (§13).

Confirm/reject are `POST` and are **not** idempotent by accident: the state
machine (§5.2) makes a second confirm a 409.

---

## 13. Testing requirements

Security-relevant paths get explicit tests (`CLAUDE.md`). Required, not
suggested.

**The governing principle, as tests**

1. A user holding `ai.act.confirm` but **not** the job type's dispatch permission
   gets 403 on confirm, and no job is created.
2. A `technician` proposing against a device the ticket does not link → refused.
3. A proposal for a catalogue row lacking the persona in `personas` → refused.
4. A proposal for a job type outside `APPROVABLE_JOB_TYPES` → refused, even if a
   legacy row names it.

**Preview binding (the IPAM lesson)**

5. Confirm with a digest that does not match the recomputed one → 409, no job.
6. Editing the `approved_action` row between preview and confirm → 409.
7. Lowering `ai_autonomy_ceiling` between preview and confirm → refused.
8. **Mutation test:** a confirm handler that takes `job_type`/`params` from the
   request body instead of the stored row must make a test fail. If it does not,
   the suite does not test the property.
9. `expires_at` passed → 410, state becomes `expired`, no job.

**The dial**

10. `off` mints no row; `suggest` mints no row; `require_approval` mints one.
11. `'autonomous'` is unstorable in `ai_autonomy_ceiling` (database CHECK, not
    application code).
12. A destructive or `high` catalogue row can never produce an auto-dispatch at
    any dial position.

**Injection, act edition**

13. §8.5's named attack, extended: the hostile line arrives through the **real
    unauthenticated email-intake ingress** (borrowing that suite's own fixture,
    as the current boundary test does), is read by a copilot turn, and the turn
    produces **no dispatched job** — at most a flagged, tainted proposal that a
    human must confirm.
14. A prior assistant answer containing flagged text is **replaced by the stub**
    in an act-capable session, and replayed verbatim in a read session.
15. A session created `mode='read'` cannot be promoted to `'act'`.
16. Model output naming a job type, params, or a device the caller cannot see
    reaches no dispatch path.

**Tenancy and boundaries**

17. Cross-org `ai_action` read → 404, never another org's row.
18. The portal AI surface registers no action route (router-derived, so a future
    addition fails).
19. `app/core/ai/` imports no `app.modules.*` package.
20. The relocated boundary suite still asserts exactly two HTTP-client
    construction sites, now across every persona package.

**Audit**

21. A dispatch writes `ai.action_dispatched` **and** the kernel's own job record.
22. No `ai.*` audit metadata anywhere contains `rationale`, question text, or
    answer text — asserted over the metadata, not over one call site.
23. The retention sweep blanks `rationale` and leaves every other `ai_action`
    column intact, including on dispatched rows.

---

## 14. Deferred (explicitly not in v1)

- **Group / multi-device previews** ("this runs on 37 devices — confirm"). Needs
  `max_target_scope` widened beyond `('device',)` — a reviewed kernel migration
  the registry's `MAX_TARGET_SCOPES` comment already anticipates — plus a blast-
  radius calculator and a per-target result view. One click authorizing 37
  model-chosen signed jobs is a different risk decision and deserves its own §0.
- **Composed campaigns** ("patch the finance group in Friday's window"). Needs
  the above plus scheduling. §0.7's narrowing is what v1 buys instead.
- **Any AI `autonomous` position** (§0.4). If it is ever wanted, it needs its own
  spec section, its own falsifiers, and a CHECK widening a reviewer sees.
- **End-user-initiated acts.** Blocked on §11.1, and additionally on §0.9's
  authority argument, which survives even after device ownership exists.
- **A machine-authored public ticket comment.** Blocked by `sla` §0.6.1's rule
  that an automated public note does not discharge a response promise, and by
  §0.3.
- **Scheduled ops briefings / proactive suggestions.** They are turns nobody
  asked for, so they need a scheduler, a cost story, and a rule about what a
  proposal minted with no human present may do. Not a v1 increment.
- **Org-editable system prompts.** `prompt.py` gives the reason: it makes the
  system prompt data.
- **A fourth persona**, of any kind, before the three here have shipped.

---

## 15. What this document changes elsewhere

Writing this file has three consequences that must be handled as part of the
first build task, not discovered later.

1. **`tests/test_ai_admin_boundaries.py::test_act_mode_is_blocked_on_a_document_
   that_does_not_exist` now fails.** It asserts `docs/modules/ai-automation.md`
   does not exist, and its own docstring says: "If that document is ever
   written, this test goes red — which is the correct moment for somebody to
   revisit §12 deliberately." This is that moment. The test should be
   **replaced**, not deleted, by one asserting the *remaining* boundary: that
   `app/modules/ai_admin/` still contains no act-mode implementation until the
   substrate exists in `app/core/ai/`. The guarantee is "act mode is not grown
   inside the admin module", and that guarantee outlives the file's absence.
2. **`docs/modules/ai-admin/spec.md` §12 and §17.11 need amendment.** §12 should
   point here for the substrate rather than describing it as unphased; §17.11's
   "this reasoning expires with read mode" is discharged by §0.8/§7.4 and should
   say so.
3. **`docs/06-roadmap.md`'s Phase 17 status note** ("Act mode is blocked on a
   document that does not exist") is now false in its premise and should be
   rewritten to "act mode is specified and unbuilt", with the three kernel
   changes (§10) named as its first tasks. The AI Automation layer should also
   be given a phase number; it is currently "not yet phased", and it now has a
   spec.

---

## 16. Migration notes

**One migration.** At the time of writing, the head is `0046`
(`0046_approved_action_fuse_ceilings.py`), so the next free number is `0047` —
**but the number must be confirmed against `backend/migrations/versions/` at
build time**, because other work lands in parallel and a guessed number is a
broken chain. Migrations are forward-only; never edit a shipped one.

It creates `ai_action` and `ai_autonomy_ceiling` with their RLS policies and
CHECKs, widens `ai_session.mode` and `ai_session.persona`, seeds the two
permissions (`ai.act.propose` to the technician tier, `ai.act.confirm` to the
admin tier, both to super-admin/org-admin so
`test_org_admin_has_all_permissions` holds), and seeds **no**
`ai_autonomy_ceiling` rows — the layer ships inert. *(The `ai_settings.enabled`
this once compared itself to was dropped on 2026-08-25 by `ai-admin` spec
§0.14; the ceilings' own deny-by-default is unaffected.)*

It writes **no** `approved_action` rows and adds **no** persona strings. An org
adopting the layer adds `'admin'`/`'technician'` to the catalogue rows it chooses,
through the existing audited write path. A migration that silently made every
health remediation AI-proposable would be this layer granting itself reach.

---

## 17. Related documents

- `CLAUDE.md` — the six security invariants.
- `docs/06-roadmap.md` — Phase 16 (the signal source), Phase 17 (the third
  persona), and the note on the AI layer.
- `docs/modules/ai-admin.md` — the scoping brief that names this layer.
- `docs/modules/ai-admin/spec.md` — read mode as built; §12 is the seam this
  document attaches to; §0.1, §0.5, §0.6, §0.7, §8, §17.11 are inherited.
- `docs/modules/endpoint-health-alerting/spec.md` §2.9, §2.10, §8.2–§8.5 — the
  approved-action registry's own design, including why it is kernel-owned.
- `docs/modules/portal/spec.md` §0.1, §0.2 — the portal principal and its
  row-level, fail-closed tenancy.
- `docs/modules/email-intake/spec.md` §0.2 — why inbound content is
  unauthenticated.
- `docs/modules/sla/spec.md` §0.6, §0.6.1 — why a machine-generated public
  comment does not discharge a response promise.
- `docs/modules/ipam/spec.md` §8.1 and `app/modules/ipam/importer.py` — the
  preview/digest shape this document deliberately does not repeat.
- `docs/03-security-model.md`, `docs/05-agent-spec.md` — signing and the agent
  side neither this layer nor any persona touches.

---

## 18. Build amendments (Tasks 1–8)

Recorded here rather than edited into the sections above, so a reader can see
what the build learned and what it decided. Each item names the section it
amends. Everything below was found by running code or a mutation, not by
reasoning about the text.

### 18.1 §4.1 — a fourth CHECK on `ai_autonomy_ceiling`

§3.1 says the `end_user` persona may never "be raised above `off` … for any risk
class" and §4.1's table gave no constraint for it, which would have left the rule
to application code — exactly what §0.4 refuses for `autonomous`. Migration 0048
adds `ck_ai_autonomy_ceiling_end_user_is_off`:
`NOT (persona = 'end_user' AND level <> 'off')`. Deleting it stores an
`end_user` row at `require_approval`, which the test SELECTs back and prints.

### 18.2 §4.2 — the v1 clamp is redundant against the database, and is kept anyway

§4.2 writes the resolution as
`min_level(ceiling, action.autonomy, 'require_approval')` and presents the third
argument as what stops an AI persona reaching `autonomous`. **Given §4.1's CHECK
it cannot be**: `CEILING_LEVELS` stops at `require_approval`, so
`min_level(ceiling, autonomy)` is already at most `require_approval` for every
ceiling the database can store. Verified by deleting the clamp and running the
dial's whole suite green.

It is kept as a second line against a ceiling read from outside
`CEILING_LEVELS` (a widened CHECK, an admin-engine fixture, a future migration),
and `tests/test_ai_autonomy.py` now carries a case that passes such a value — so
the deletion is observable. The database CHECK remains the guarantee.

### 18.3 §10 — a fourth kernel change: the rate fuses move to the kernel

§6.1 requires the preview to *read* the three rate fuses and §6.5 requires the
dispatch to *gate* on them, and the only implementation was
`app/modules/health/actions.py::_fuse_breach` — a private function in a business
module that §2.3 property 1 forbids `app/core/ai/` from importing. The body
moved verbatim to `app/core/approved_actions.py::fuse_breach`; health calls it
through a one-line delegation and no `test_health_*` file was edited. Two
derivations of "is this fused" is the defect `is_destructive`'s docstring exists
to prevent.

### 18.4 §6.4, §6.5 — where the two tunables live

Both sections say "org-configurable, CHECK-bounded" without saying where. They
are columns on `ai_settings`: `action_expiry_s` (default 900, `BETWEEN 60 AND
86400`) and `max_proposals_per_turn` (default 3, `BETWEEN 1 AND 10`), beside the
four tunables that already carry ceilings on that table — every write to which is
already `ai.admin` and already audited.

### 18.5 §6.4 — an expired confirm does NOT set `state='expired'` itself

The implementation plan said the row "becomes `expired`" on the 410 path. §6.4 is
right and the plan was not: `confirm_action` raises, its caller's tenant
transaction rolls back, and a `state = 'expired'` written there would be rolled
back with it — leaving a transition a reader believes in and the database never
performs. The expiry sweep is what moves the row; every confirm re-checks the
clock, so a row sitting `previewed` past its expiry is not confirmable meanwhile.
Asserted both ways.

### 18.6 §5.1 — the denormalization does not make the record survive a catalogue delete

§5.1 denormalizes `approved_action_key` and `job_type` "so the record survives a
catalogue delete". It does not: `ai_action.approved_action_id` is a NOT NULL
foreign key with no `ON DELETE` clause, which Postgres treats as NO ACTION, so
**the DELETE is refused** while any `ai_action` references the row. This build
does not change it — an `ai_action` naming a policy nobody can look up is worse —
but the surviving behaviour is "refused", not "the record survives", and the
confirm path's "no longer exists" branch is unreachable through an ordinary
delete. Both are pinned by a test.

### 18.7 §0.11, §10.2 — `NO_HUMAN_DISPATCH_PATH` has three entries, not one

The plan predicted `health.policy.apply` alone. `MODULE_GOVERNED_REASONS` has
three messages that name no permission because no human route exists:
`health.policy.apply`, `agent.cert.renew` and `agent.cert.install`. All three are
in `NO_HUMAN_DISPATCH_PATH` with their stated reason, and
`dispatch_permission_for` returns `None` for each — which every caller treats as
a refusal.

### 18.8 §2.2 — `PersonaProfile` carries its package

§2.2 names five fields. A sixth, `package`, is required so §13 test 20's "across
every persona package" can derive its scan roots from the live `PERSONAS`
registry instead of from a literal list — a literal means the fourth persona is
unscanned, which is the same defect as a denylist. `register_persona` refuses a
profile whose declared package does not contain the module registering it, so
the field cannot be quietly wrong and make every source scan pass against the
wrong directory.

### 18.9 §12 — the act tool is built per turn, not registered in a persona's tool set

`ActTool` needs the session id, the turn's `message_seq`, the acting user and
this turn's taint — facts about ONE turn. A tool object carrying them inside
`PersonaProfile.tools` would carry one turn's binding into the next, and
`message_seq` would stop describing where a proposal came from. It also keeps
`PersonaProfile.tools` a mapping of `ReadTool` only, which the boundary suite
asserts.

---

## 19. Build amendments (Tasks 9–16)

The second half of the build, recorded the way §18 records the first: each item
names the section it amends, and everything below was found by running code or a
mutation rather than by reasoning about the text.

### 19.1 §6.1 step 6 — a real defect: the ticket bound failed OPEN when the ticket had no device

`propose`'s step 6 shipped (Task 7) as
`if binding.ticket_device_id is not None and binding.ticket_device_id != device_id`.
That expression **cannot distinguish two different situations**: a persona with
no ticket context at all (the admin persona, correctly unbounded) and a
conversation that IS about a ticket whose `device_id` is NULL. §3.2 requires the
second to propose nothing — "if the ticket has no linked device, the copilot
cannot propose — it can only say so". The code skipped the check entirely, so a
copilot turn on a device-less ticket could prepare an action against **any device
its technician could see**, which makes "the ticket" decorative in exactly the
case §3.2 names.

Found by `tests/test_ai_copilot.py::test_a_ticket_with_no_linked_device_can_
propose_nothing` on its first run. The fix is a second field,
`ProposeBinding.ticket_bound`, which says the binding APPLIES — so a missing
device is a refusal rather than an absence. A ticket-bound persona now refuses
twice: once for a ticket with no device, once for a device the ticket does not
name.

### 19.2 §0.8 — the replay policy is the PROFILE and the SESSION MODE, not the profile alone

The implementation plan said "for a session whose **profile** has
`replay_policy="neutralize_flagged"`". One profile serves both of a persona's
session modes — the admin persona's read sessions and its act sessions share
`ADMIN_PROFILE` — so a policy read off the profile alone neutralizes read mode
too, and §13 test 14 requires the opposite: the SAME flagged answer replaced by
the stub in an act-capable session and replayed **verbatim** in a read one.

§0.8 states it as a property of the session ("for a session whose `mode` is
act-capable") and that is what is implemented:
`orchestrator.replay_policy_for(profile, mode=...)`. The profile field is the
persona's opt-in; the mode is the condition. `ADMIN_PROFILE` and
`COPILOT_PROFILE` opt in; `RESOLVER_PROFILE` does not, because it cannot act at
any dial position, so read mode's argument never expires for it — and a stub
there would edit what a customer was told about their own ticket.

### 19.3 §0.8, §5.3 — `mode` and `persona` are refused at the ORM layer, not only unrouted

"There is no promotion path" was true of every caller written so far, which is
not the same as a property. `AiSession` now carries a `@validates("mode",
"persona")` hook that raises on any reassignment after the value is set. It
fires on application writes only — SQLAlchemy's loader does not trigger
validators — so re-reading an `act` session is not a promotion, and what it
catches is `row.mode = "act"`, which is the line that would ship. `persona` is
included for the same reason one field over: it is the key the dial, the audit
events and every `ai_action` row resolve against.

### 19.4 §5.2 — there is no single "result path", and settle hooks three

§5.2 says `settled` is "set by the same result path that already updates `jobs`,
so this layer runs no poller", and assumes one such path. There are **three**
terminal writers in `JobEngine` — `complete` (succeeded/failed), `refuse` and
`expire_due` — reached from two different processes (the gateway and the expiry
beat). Hooking only `complete` leaves a refused or expired job's `ai_action`
sitting in `dispatched` forever, which is a lie about a signed job's outcome on
the confirmation history. All three call one body,
`JobEngine._settle_ai_action`, which delegates to
`app/core/ai/sweep.py::settle_action_for_job`; the AI layer still runs no poller.

**This is a FIFTH kernel change** (§10 names three; §18.3 added the fourth). It
is small — one private method and three call sites — but it is `app/jobs/`, and
it is the only place in this build where the AI layer required an edit to the
job engine.

### 19.5 §8.2 — `ai.action_expired` is `actor_type='system'`, not `'user'`

§8.2 says "`actor_type='user'` throughout, `actor_id` = the proposing or
confirming human". A clock has neither. Attributing an expiry to the human who
proposed the action would put a decision they did not make under their name, in
the one log nobody can correct. The kernel's own `jobs.expired` is
`system`/`None` for the same reason, and that is what the sweep writes. The event
additionally carries `persona`, `job_type` and `state_before` — all bounded
identifiers — so an operator can tell a crashed `proposed` row from an unanswered
`previewed` one.

### 19.6 §5.1, §8.3 — the retention sweep had to learn about `RESTRICT` before production did

`ai_action.session_id` is `ON DELETE RESTRICT`, and the transcript retention
sweep deletes emptied `ai_session` rows. The first org to use act mode would have
made that `DELETE` raise `ForeignKeyViolationError` — which `sweep_all_orgs`
CONTAINS, silently, leaving that org's transcript rows deleted and its sessions
accumulating forever with nothing but a log line to say so. `retention.sweep_org`
now excludes sessions that carry an action, counts them separately
(`sessions_retained`), and blanks their rationales instead. RESTRICT is not
conditional: a session whose actions are all `rejected` is equally undeletable.

### 19.7 §12 — `POST /ai/sessions` gains `mode` and NOT `persona`

§12's route table writes "`POST /ai/sessions` (gains `mode`, `persona`)". `mode`
is implemented; `persona` is not, deliberately. Each persona's router IS that
persona's surface and passes its own `PersonaProfile.name`. A client-supplied
persona would let an administrator open a session LABELLED `technician` that is
served by the admin persona's tool set and system prompt — a persona name that
lies, on the column the autonomy dial, the audit events and every `ai_action`
row are keyed by.

### 19.8 §0.2 — the resolver's executor refuses off-surface tools structurally

§0.2 argues for two executors on the grounds that the portal one "cannot
construct a staff request because it has no `AuthContext` to build one from".
Both executors in fact take a bearer string, so that argument alone does not
distinguish them at runtime. `executor_portal.invoke_portal_tool` therefore
carries an explicit guard: a tool whose path is not under `/api/v1/portal/`, or
whose gate is not a `portal.*` code, is refused **before a URL exists**. Path and
gate are checked together because either alone is defeatable — a staff route
mounted under `/portal/` would pass a path check, a portal-gated route elsewhere
would pass a gate check.

The loopback choice itself is recorded per the plan: `resolve_portal_principal`
and `require_portal_permission` re-run verbatim and `portal_session` is opened by
the ROUTE, so this layer never decides which GUCs get pinned. **The premise is
that portal principals carry a bearer token.** `principal.py` says external end
users will need a second `resolve_portal_principal`; if that lands without a
token, the correct move is to report it, never to mint a service token for the AI
layer.

### 19.9 §2.1 — the read-tool registry is platform capability living in a persona package

`app/modules/ai_admin/tools/registry.py`'s own docstring says a read tool is
"PLATFORM CAPABILITY, authored by the platform", as against `approved_action`,
which is org policy. It nonetheless lives in the admin persona's package, so the
technician copilot's `tools.py` imports the `ReadTool` objects from it — one
persona package depending on another, which is not what §0.1 wants.

The alternative was copying the two `ReadTool` definitions, each carrying a
`response_fields` frozenset that only the admin persona's drift test compares
against the live response model. A second copy is a second list that test does
not check, so the day another module adds a field, the admin persona's build goes
red and the copilot keeps serving the new field to a model — precisely the defect
`registry.py` exists to prevent, reproduced by hand.

**The right fix is moving the registry to `app/core/ai/`**, with
`test_ai_admin_registry.py`, `test_ai_admin_boundaries.py`'s `PACKAGE_ROOT` and
vacuity floor, and `test_ai_boundaries.py`'s `_FORBIDDEN_GATES` import following
it. That is its own reviewable change and this build did not make it.

**CLOSED — see §19.16**, which records the move, every re-aimed guard with the
mutation proving it still bites, and the new guard that stops one persona
importing another again.

### 19.10 §12 — the copilot's ticket binding is per TURN, not per session

`ai_session` carries no `ticket_id`, and §5.3 says the only changes to that table
are `mode` and `persona` — so this build did not add a column the spec forbids.
The consequence: a technician can carry one copilot conversation across two of
their own tickets. Nothing is gained by doing so (they hold `ticket.read` on
both), and the security-relevant binding — which device may be proposed against —
is re-derived from THIS turn's ticket every time, so it is never stale. What it
costs is transcript hygiene, by the technician's own hand. A `ticket_id` column
would fix it and needs a migration and a §5.3 amendment.

**ASSESSED IN §19.20: an acceptable design consequence, not a defect — with one
real gap beside it, in the RECORD rather than in the check.**

### 19.11 §15.1 — what the re-aimed boundary guard now asserts

`tests/test_ai_admin_boundaries.py::test_no_source_file_defines_an_act_mode_name`
was a SUBSTRING scan over class, function, module-assignment **and `ast.arg`**
names, against a list containing `"ai_action"` and `"autonomy"`. Run unchanged
against the admin persona's act routes it names **nine** legitimate things:
`ai_action_id` (a path parameter §12 requires), four handler names, and
`approved_action_key`/`approved_action_id`/`effective_autonomy` as response
fields §6.2 requires. That is the constraint-10 failure mode exactly — a guard
matched against mentions pushes the next reader to delete the guarantee.

It now matches EXACT names against **top-level definitions only** (class,
function, module-level assignment), never against arguments and never inside a
class body: a pydantic field declaring `preview_digest` is a declaration of what
the shared layer computed; `preview_digest = sha256(...)` at module level would
be this router computing one. Two new clauses carry the part that matters:

- `test_the_act_machinery_is_imported_from_the_kernel_and_not_reimplemented` —
  a name scan cannot see `def _work_out_the_digest(...)`, but it can see that the
  package reaches the act path through `app.core.ai.act`;
- `test_this_package_writes_no_ai_audit_event_and_creates_no_job` — no
  `audit_service.record`, no `job_engine.create(`, no `.stream_turn`, and no
  import of `app.core.audit`, anywhere under the package.

`test_the_only_persona_this_package_writes_is_admin` derives the other personas'
names from `app/models/ai.py::AI_PERSONAS` (a compiled-in constant, complete
whatever has been imported) rather than from the live `PERSONAS` registry, which
a run that imported only this persona would find one-long.

### 19.12 §13 test 22 — the audit test that is not a grep

"No `ai.*` audit metadata contains `rationale`, question text or answer text" is
a claim about metadata dicts assembled in six places. `tests/test_ai_act_
structure.py` drives a real turn and a real proposal with three DISTINCT
sentinels — one per prose channel, so a failure names which one leaked — asserts
each is where it belongs (transcript, `ai_action.rationale`), then SELECTs every
`audit_events` row for the org and searches `meta`. Adding `rationale` to
`ai.action_proposed`'s metadata makes it fail naming the event and the sentinel.

### 19.13 What is verified, and what is not

- **The idle budget is untouched and unmeasured, correctly.** This layer added
  no agent code, no job type and no capability, so there is nothing new to
  measure. `agent/` is not in this build's diff.
- **The live smoke test (§16 of the plan) was NOT run.** It needs a real enrolled
  device and a real model provider, and this environment has neither. Every step
  of it is covered by an automated test against the real routes with a scripted
  provider — `tests/test_ai_act_api.py::test_propose_preview_confirm_dispatch_
  over_http` asserts the job, the five audit events and `requested_by`; the dial
  at `off` is `tests/test_ai_act_propose.py`'s §13 test 10 — but a scripted
  provider is not a model and an in-process loopback is not a fleet. That gap is
  stated rather than closed.
- **`ai.action_expired`'s `age_s` is the age of the PROPOSAL**, not the time past
  the deadline: "how long did this sit unanswered" is the question an operator
  asks and it is comparable across orgs whose `action_expiry_s` differ.

### 19.15 §6.4 — the digest detected a catalogue edit only to the nearest SECOND

`digest_fields`' `approved_action_updated_at` was formatted by
`app/jobs/canonical.py::rfc3339`, which renders `%H:%M:%SZ` — **truncated to
whole seconds**. `ApprovedAction.updated_at` carries `onupdate`, so an edit
landing in the same wall-clock second as the row's previous update produced an
IDENTICAL digest and the confirm proceeded against the edited row. §6.4's claim
— "editing the catalogue invalidates the confirmation" — was therefore true only
at one-second granularity.

The window is small and nobody can steer it, which is why nothing caught it for
eight tasks. What caught it in the end was a **flake**:
`tests/test_ai_act_confirm.py::test_a_fuse_that_passed_at_preview_and_is_
breached_at_confirm_refuses` passed alone and failed in a full-suite run,
because under load the preview and the catalogue edit straddled a second
boundary — so a test about the FUSE received the CATALOGUE-EDITED conflict
instead. Its docstring asserted the opposite ("nothing about the digest's ten
fields changes, because `cooldown_s` is not one of them"), which is wrong twice
over: `approved_action_updated_at` IS one of the ten, and `onupdate` moves it on
any write.

**Both halves are fixed.** The field is now an `isoformat()` string (microsecond
precision, still a `str`, so constraint 9's `canonical_bytes` requirement is
satisfied), and the fuse test recomputes the digest after the edit so the only
thing left that can refuse is the second fuse read. A new test —
`test_ai_act_digest.py::test_a_catalogue_edit_inside_one_second_still_moves_the_
digest` — moves `updated_at` by ONE MICROSECOND inside the same second and
asserts the two digests differ; restoring `rfc3339` makes it print two identical
hashes.

**The cost, stated:** a `preview_digest` stored by a previous release does not
match a recomputation by this one, so previews in flight across the deploy are
409s. That is fail-closed — a refusal, never a dispatch — and they expire within
`action_expiry_s` anyway, which a CHECK bounds at 24 hours.

**The general lesson, which is not about this field.** `rfc3339` exists to make
SIGNED JOB BYTES unambiguous and its truncation is correct there. It was reached
for here by adjacency, because constraint 9 says "every datetime is
`rfc3339(...)`" — advice about the *type*, applied as advice about the
*formatter*. Any future digest field carrying a timestamp needs the same
question asked: is second granularity enough to detect the change this field
exists to detect?

### 19.16 §2.1, §19.9 — the read-tool registry moved to the kernel, and one persona stopped importing another

§19.9 named the fix and this closes it. `app/modules/ai_admin/tools/` — the
allowlist, its argument models and their re-export — is now
`app/core/ai/tools/`, so `ai_copilot/tools.py` imports `REGISTRY` from the
kernel instead of from the admin persona's package. The `ReadTool` objects are
still **shared, not copied**: §19.9's whole argument is that a second
`response_fields` frozenset is a second list the drift test does not check, and
`tests/test_ai_copilot.py` asserts object IDENTITY (`tool is
PLATFORM_READ_TOOLS[name]`) rather than equality so a copy fails rather than
passing quietly.

No table, no migration, no route and no behaviour changed: the same two tools,
the same gates, the same `response_fields`, under the same drift test.

**The test churn §19.9 predicted, and the shape of each re-aim.** Every guard
below was re-pointed at what it was always about, and each was proved to still
bite by mutation:

| Guard | Re-aimed to | Mutation that makes it fail |
|---|---|---|
| `test_ai_admin_boundaries.py::_source_files` vacuity floor | 8 → **5** (measured: `__init__`, `api`, `schema`, `prompt`, `profile`) | floor at 6 fails naming "only 5"; a wrong root fails naming "it is vacuous" |
| `test_every_registry_entry_is_a_read_tool_whose_method_is_get` | `test_this_personas_tool_set_holds_only_read_tools_whose_method_is_get`, over `ADMIN_PROFILE.tools` | `method="POST"` on `health.alerts` → "health.alerts is a POST tool" |
| `test_no_registered_tool_is_gated_on_a_credential_permission` | `test_no_tool_this_persona_holds_is_gated_on_a_credential_permission`, over `ADMIN_PROFILE.tools` | `permission="credentials.read"` → "health.alerts is gated on credentials.read" |
| `test_the_allowlist_and_both_denylists_are_module_constants` | **moved** to `test_ai_boundaries.py`, scanning `app/core/ai/tools/registry.py` | `os.getenv` in the registry → "calls os.getenv"; a wrong filename → "the allowlist moved again and this scan is asserting nothing" |
| `test_ai_admin_registry.py` | **renamed** `test_ai_read_tools.py`; its two package-scoped tests moved into `test_ai_admin_boundaries.py` | the approved-action guard still names `get_approved_action` from its new home |
| `test_ai_boundaries.py::test_no_persona_tool_is_gated_on_a_credential_permission` | `_FORBIDDEN_GATES` imported from the kernel | (unchanged in shape) |

A module-scoped file asserting a kernel constant's shape asserts nothing about
that module, which is why the two tool-set guards now read `ADMIN_PROFILE.tools`
— what THIS persona hands the turn loop — rather than the kernel mapping it
currently happens to be.

**One guard is NEW, and it is what stops the finding recurring:**
`test_ai_boundaries.py::test_no_persona_package_imports_another_persona_package`.
Moving the file removed the instance; nothing removed the *possibility*, and the
original defect was invisible precisely because nothing failed. The package list
is derived from `PERSONAS` (never a literal), so the persona added next is
covered the day it registers. It also resolves `from app.modules import
ai_admin` — the alias form, which the existing `_imported_modules` helper
reports as `"app.modules"` and would have missed; both forms were mutated and
both are named.

**Two stale references remain, in files this work does not own:**
`app/modules/reporting/registry.py` and three `tests/test_reporting_*.py`
docstrings still cite `app/modules/ai_admin/tools/registry.py` and
`tests/test_ai_admin_registry.py` as prior art. They are prose, not imports;
nothing depends on them resolving.

### 19.17 §6.1 step 5 — a real defect: the device visibility probe failed OPEN

`_device_is_visible` shipped as `kind not in {"forbidden", "not_found"}` — a
**denylist over two of `ToolErrorKind`'s eight members**. The probe's SUCCESS
signal is `blocked` (`_DEVICE_PROBE` declares an empty `response_fields`, so a
device the caller may see comes back 200 and the scrubber refuses the body), so
the expression collapsed to "did we get an explicit 403 or 404" — not "did we
get an answer".

Verified by execution before the fix: `upstream_error`, `timeout`,
`session_expired` (a 401), `unknown_tool` and `invalid_arguments` all returned
`visible=True`, and so did a `ToolResult`, which has no `kind` at all and fell
through `getattr(outcome, "kind", None)`. **Any failure to obtain an answer was
a yes.** That inverts invariant #6: authorization is server-side, and a server
that could not answer has authorized nothing. Both call sites are affected —
§6.1 step 5's propose and §6.5's re-check at confirm, which is the one that
stands between a stored proposal and `job_engine.create`.

**The fix is the allowlist: `kind == "blocked"`.** Written that way so the
DEFAULT for a `ToolErrorKind` added later is refused, rather than a longer
denylist that a ninth kind would silently escape.

**The tests that shipped with the defect were decoration, and the reason is
worth naming.** Inverting the check to the correct code left every one of them
green, because each drove a case that produces `forbidden` or `not_found` —
the two the code handled. Three tests replace them:

- `test_only_a_blocked_probe_grants_visibility` **parametrises over every
  member of `ToolErrorKind`, read off the type with `get_args`**, not over the
  kinds the code names. A literal list here would reproduce the defect inside
  the test. It asserts the pair `(kind, visible)`, so the mutation back to the
  denylist prints `('invalid_arguments', True) != ('invalid_arguments', False)`
  rather than `DID NOT RAISE`.
- `test_a_probe_that_somehow_succeeds_does_not_grant_visibility` covers the
  non-`ToolError` outcome the `getattr` default waved through.
- `test_the_probes_success_signal_really_is_blocked` pins the fact the
  allowlist RESTS on, against the real route: `(kind, status) == ("blocked",
  200)`. Without it an allowlist over a kind the probe never returns would
  refuse everything, and the reason would be four files from the failure —
  mutating the check to `== "redacted"` makes the real propose path answer "no
  such device", which is what that mutation is for.

### 19.18 §6.4 — a real defect: the digest was a client-side invariant with a server-side name

`confirm_action` computed `recomputed = compute_preview_digest(row, action)` and
compared it against **the digest the CALLER sent**. `row.preview_digest` — the
digest the server itself stored when it issued the preview — was never read on
the confirm path at all.

So §6.4's invariant ("the row has not changed underneath the human") was
enforced by a value the client supplies. A caller able to compute the current
digest could confirm an act whose catalogue row had been edited after the human
saw it, and the server would agree with itself.

**Demonstrated by execution, and the demonstration was already in the repo.**
`test_a_fuse_that_passed_at_preview_and_is_breached_at_confirm_refuses`
recomputed the digest over an edited catalogue row *specifically to get past the
digest gate*, so that it could test the fuse gate behind it — a test in this
suite using the bypass, on purpose, and passing. A new test does the same thing
adversarially (edit the catalogue, confirm with a freshly recomputed digest) and
against the shipped code it **dispatched a real job**: `(jobs, state) == (1,
'dispatched')` where the refusal is `(0, 'previewed')`.

**The fix is two comparisons, both against `row.preview_digest`** — §6.4 as
amended above. The fuse test no longer needs the bypass: `cooldown_s` is part of
the world from the start, the preview is taken with the fuse open (verdict
`None`, asserted), and a job seeded afterwards trips it — the catalogue row is
never written, the digest never moves, and the stored digest still validates, so
a refusal there cannot be the digest gate wearing the fuse's clothes.

**What each mutation proves**, since with both checks present the two are
interchangeable for the *verdict*:

- deleting check 1 → the catalogue-edit test dispatches;
- deleting check 2 → the wrong-action's-digest test dispatches;
- reversing the two → the recompute test prints the digest-bookkeeping message
  instead of the catalogue one, which is the ordering the operator-facing
  message depends on.

**Cost, stated:** a row whose `preview_digest` is `''` (the column's server
default, i.e. proposed before the column was populated) now fails check 1 and is
not confirmable. Fail-closed, and already the stated cost of §19.15's format
change.

### 19.19 §12 — reading a proposal costs what reading a device costs

`GET /ai/actions` and `GET /ai/actions/{ai_action_id}` shipped gated on `ai.use`
alone, org-wide, exactly as §12's table said. That table was wrong about the
price.

`ActionOut` carries `target_device_id`, `job_type` and `approved_action_key`;
`ActionPreviewOut` adds the target's **hostname**, the rendered params and the
model's `rationale`. So a principal holding `ai.use` and not `devices.read`
could learn that a device exists, what it is called, and which approved action
somebody prepared against it — from a queue that lists **every persona's**
proposals in the org, including the technician copilot's, which are proposed
inside tickets.

**`devices.read` is the right second gate, not an arbitrary one.** Device
visibility in this platform IS org membership plus that code: it is what the
confirm path's loopback probe re-runs (`act._device_is_visible`), and what the
`devices.health` read tool is gated on. "Scope the list to devices the caller
can see" and "require `devices.read`" are therefore the same requirement today,
and the second needs no per-row probe.

Checked in the HANDLER, not on the route, for the reason
`app/modules/ai_copilot/api.py` already states: `require_permission` carries
exactly one code and `route_permission` recovers it by reflection, which is what
makes RBAC completeness derivable from the router. On the detail route the
refusal comes BEFORE the row is loaded, so it is not an existence oracle over
`ai_action.id` either.

**The test has three cases, and the third is what makes the other two mean
anything**: `ai.use` + `devices.read` → 200 *and the row is returned*, `ai.use`
alone → 403, `devices.read` alone → 403. Without the last, a handler that had
simply started refusing everybody would pass; without the first, so would one
that refuses nobody. The assertion is the tuple `(list status, detail status,
ids returned)`, so dropping either check prints which route leaked.

**When §11.1's device ownership lands this changes.** "The devices this
principal may see" stops being org-wide, `devices.read` stops being equivalent
to it, and this gate has to become a per-row filter. Written down here rather
than left for somebody to notice.

### 19.20 §19.10 — the per-turn ticket binding, assessed: acceptable, and the gap was in the record

**Verdict: the per-turn binding is an acceptable design consequence, and no
`ai_session.ticket_id` is needed. §5.3's constraint is not what makes it a
defect, because it is not a defect.**

**The evidence, and it is an observation rather than an argument.** Every
security-relevant decision in `create_copilot_turn` reads THIS turn's ticket and
nothing on the session: the route gate (`ticket.read`), the org-predicated
ticket read, `ticket_device_id`, and `ticket_bound`. Nothing in `propose` reads
session state either — `ProposeBinding` is built per turn and §6.1 step 6 reads
only its two ticket fields. So a conversation carried across two tickets is
bound to the second one the moment it moves.

§19.10 asserted exactly that and nothing tested it. Two tests now do, and the
gap they close is specific: **every other copilot test opens a fresh session per
turn**, so a change that hoisted the ticket resolution out of the turn route —
the obvious "one query, not three" tidy-up — would have gone unnoticed by all of
them.

- `test_the_bound_is_this_turns_ticket_and_not_the_one_the_session_opened_on` —
  ONE session, three turns, two tickets naming two different devices. Turn 2
  proposes the device the SESSION opened against, under a ticket that does not
  name it, and is refused; turn 3 proposes the new ticket's device and is
  previewed. Asserted as the ordered list of `(target_device_id, state)`
  actually written, so "the binding is carried on the session" and "the route
  refuses every continued session" print different values.
- `test_a_second_turn_on_a_device_less_ticket_proposes_nothing` — §19.1's
  fail-open, re-asked on a LATER turn. A carried-forward `ticket_bound` or
  `ticket_device_id` is precisely how that defect would come back, and the first
  turn of this session IS bound to a ticket with a device, so the fixture makes
  the carried and re-derived cases differ.

**What the carry-over actually costs** is unchanged from §19.10 and is not
security: transcript hygiene, by the technician's own hand, over tickets they
already hold `ticket.read` on. Taint carries forward with the transcript, which
is the conservative direction.

**The one real gap: nothing durable said WHICH ticket authorized a proposal.**
`ai_session` has no ticket by §5.3, `ai_action` records `session_id` and
`message_seq`, and the ticket is the entire justification for the one device a
ticket-bound persona may name. Invariant #4 asks for the privileged action to be
recorded; a proposal whose authorizing constraint is unrecorded is recorded
halfway.

Closed within §5.3: `ai.action_proposed`'s metadata gains `ticket_id`, carried
on `ProposeBinding` and read by no check. An audit event is per-turn, which is
exactly the scope of the binding it describes — a column would have had to
choose between the session's ticket and the turn's, and the session does not
have one. The key is **absent** for an unbounded persona rather than null:
`"ticket_id": null` on an admin proposal reads as "this was about a ticket and
we lost it", and presence is then what distinguishes a bound proposal from an
unbound one without a reader having to know which personas are ticket-bound.

**`ai_action.ticket_id` was considered and NOT taken.** §5.3 does not forbid it
— `ai_action` is this module's own table — so the reason is not the spec. It
would put the ticket on the confirmation screen, and §6.3 makes that screen's
layout a security property: what the confirming human weighs is catalogue-derived
fact, and a ticket subject is text a requester wrote. If a later build wants the
ticket shown there, it needs §6.3 revisited on purpose, migration **0052**, and
a decision about which region it renders in — not a column added for an audit
question the audit log now answers.

### 19.14 What is still deferred, and what is still blocked

Restated here so the next reader does not re-derive it from §11 and §14.

**Still blocked, unchanged by this build:**

- **§11.1 device ownership.** `devices` still has no owner, primary-user or
  assigned-to column; `device_tags` is still free key/value with no semantics;
  the only device↔person link is `ticket.device_id`, nullable and asserted by
  whoever created the ticket — for an email-created ticket, by a parser reading
  text an unauthenticated sender wrote. This blocks **end-user-initiated acts**,
  permanently, until it exists. It is now a TESTED property rather than an
  intention: `tests/test_ai_portal.py::test_the_tool_set_reaches_no_device_no_
  runbook_and_no_staff_surface` fails on a device tool in the resolver's set.
  **A builder who believes they need device ownership has hit §11.1 and must
  stop and report, not derive it from `ticket.device_id`.**
- **§11.2 a published knowledge base.** `doc_articles` still has no
  audience/visibility/published dimension and all 15 documentation routes are
  still gated `docs.read`/`docs.manage`, so the corpus is an internal runbook
  store. The resolver therefore cannot say "here is how to reconnect the VPN".
  The precondition is **documentation-module work**: an audience dimension plus a
  portal-facing read route. This layer must not invent a second article store and
  must not widen a `docs.read` route to portal principals; the same test above
  fails on a documentation tool. *Resolved 2026-09-23 -- see §11.2.*
- **§11.3 a ticket-deflection surface.** The portal still has no "search before
  you file" step, so the resolver is placed in-ticket only. Not blocking; it
  bounds the persona's commercial value and this build did not invent a surface
  to stand on.

**Still deferred, unchanged (§14):** group/multi-device previews, composed
campaigns, any `autonomous` position, end-user-initiated acts, a
machine-authored public ticket comment, scheduled ops briefings, org-editable
system prompts, and a fourth persona.

**Two things this build could have taken and did not:**

- **No migration.** Tasks 9–16 added no table and no column. `ai_session` gained
  no `ticket_id` (§19.10) and no promotion column; the two act tables and the
  dial were `0048`/`0049` and are unchanged. The next free migration number is
  therefore untouched by this half of the build.
- **No agent code, no job type, no capability, no wire-protocol change.**
  `agent/` is not in this build's diff, so the idle budget is untouched — and
  correctly unmeasured, because there is nothing new to measure.
