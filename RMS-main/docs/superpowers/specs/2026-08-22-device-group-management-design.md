# Device group management — create, update, delete — design

**Date:** 2026-08-22
**Layer:** platform kernel (`app/api/v1/device_groups.py`, `app/services/device_groups.py`,
`app/modules/inventory/rules.py`), plus one console surface.
**Follows:** `787461f`, which shipped `GET /api/v1/device-groups` and closed its
own commit message with *"nothing creates a device group"*. Recommendation 3 of
`docs/superpowers/audits/2026-08-21-ux-remediation-closeout.md` is closed on the
read side and open on the write side.
**Status:** design. Three decisions are marked **OPEN** in §10 and need the
operator before the first line of code.

---

## 1. The problem, stated exactly

`GET /api/v1/device-groups` returns this org's groups, each with the id every
group-scoped act in the platform takes. On a real deployment it returns
`{"items": [], "total": 0}` and always will, because **no code path in
`backend/app/` constructs a `DeviceGroup`**. Verified again for this spec: the
only `DeviceGroup(` constructors in the repository are in `backend/tests/`
(`test_device_groups_api.py`, `test_inventory_rules.py`, and the health and
reporting fixtures), and no migration seeds a row.

**The second half is worse and was not in the commit message.** There is no way
to put a device *into* a group either. `AssetCreateRequest` and
`AssetUpdateRequest` (`app/api/v1/devices.py:37,60`) do not carry
`device_group_id`; `DeviceOut` returns it (`:87`) and nothing writes it. The one
code that does write it is `inventory.rules.evaluate_rules`, from a rules
document that also has no editor. So the column that carries the platform's
entire notion of "which machines" is written by exactly one path, fed by a
document nobody can author.

And `devices.manage` — a permission code seeded by migration `0003`, whose
description reads *"Create, edit, **group**, and decommission devices"* — gates
**no route anywhere in the codebase**. Grep returns the catalogue entry, the
seed, and one docstring example. It has been dormant since Phase 0.

This is not CRUD, for three reasons this spec has to face rather than route
around.

**A group is the targeting and scoping primitive.** `deploy/dispatch.py:157`
and `deploy/scripts.py:191` resolve a `target_type="group"` job to
`Device.device_group_id == target_id`; `bcp/service.py:289` resolves a
`{"kind": "group"}` scope entry the same way; `patch`, `backup` and `remote`
each key a policy on `device_group_id`; `health/resolution.py` binds a profile
to a group and walks `parent_group_id` **upward**; `user_roles.device_group_id`
narrows an RBAC grant. Creating, renaming or removing a group is therefore a
change to what jobs hit, what policy applies, and who may act.

**`NULL` is not "no group", it is the org default.** `patch/policy.py:69`,
`backup/policy.py:72`, `remote/policy.py:59` and
`health/resolution.py::_org_default_assignment` all read `device_group_id IS
NULL` as the fallback policy. A device leaving a group does not lose policy; it
*changes* policy, silently, to whatever the org default says. That is why
`ungrouped_device_count` is on the list envelope, and it is why deletion in §7
is the hardest section here.

**Name order is dynamic-membership precedence.** `rules.py:83-84` walks
rule-bearing groups `.order_by(DeviceGroup.name)` and the first match claims the
device. Renaming a group therefore re-orders the evaluator and can move machines
between groups — and hence between patch, backup and remote policies — with no
edit to any rule. §6 is about that.

---

## 2. What the code does today

Every claim below was read from the source for this spec, not carried over.

**The model** (`app/models/core.py:111-122`). `device_groups` is
`id, org_id, name String(200), parent_group_id (self-FK, nullable), description
Text, rules jsonb NULL`, with `UniqueConstraint("org_id", "name")` and
`TimestampMixin`. There is **no** cycle constraint on `parent_group_id`, no
depth bound, no ordering column and no soft-delete column.

**The evaluator** (`app/modules/inventory/rules.py`). Called from exactly one
place: `inventory/ingest.py:256`, step 7 of a successful ingest. Its shape:

- Rules document: `{"all": [{"fact": <path>, "op": <op>, "value": <v>}]}`,
  every condition must hold. Ops are `eq`, `ne`, `contains` (case-insensitive
  substring), `gt`, `lt` (numeric, via `float()`).
- Fact paths: the flat set `hostname`, `os_family`, `os_version`,
  `architecture`, `ram_bytes`, `serial`, plus `facts.<key>` and
  `software:<name>`.
- **It reads the inventory *document*, never the device row.** `_fact_value`
  takes a `Device` parameter and does not use it. This decides §5.3: a
  re-evaluation outside the ingest path has no live document and must replay a
  stored one.
- **An unknown fact path is not an error.** `_fact_value` returns `_MISSING`;
  `_condition_holds` then returns `False` for every op **except `ne`**, which
  returns `True`. So a typo'd fact under `ne` matches every device in the org.
  This is the single most dangerous property of the document today.
- A malformed document raises inside `_matches`, is caught, logged with the
  group id, and **skipped** — grouping must never break ingestion. Correct, and
  it means a rule the API accepts but the evaluator cannot evaluate fails
  *silently in a log line* rather than at the point of authorship.
- `select(DeviceGroup).where(DeviceGroup.rules.is_not(None))` carries **no
  `org_id` predicate**. It is safe today because ingest runs inside
  `org_session` (`gateway/main.py:316` and friends) and RLS scopes it. A sweep
  written on the admin engine — the shape four of `worker.py`'s retention
  sweeps already use — would cross-assign devices between tenants. §8 makes
  this a requirement rather than a note.
- A match **overwrites a hand placement**. The loop assigns
  `device.device_group_id = group.id` whenever a rule matches, whatever was
  there before. Only the *no-match* case preserves it, which is all
  `test_no_match_preserves_manual_placement` covers.
  **`docs/modules/inventory/spec.md` §7 is wrong where it says "rule groups
  never unassign manual placements — only moves between rule groups or from
  NULL are automatic".** Correcting that sentence is part of this work (§9).

**Nothing schedules an ingest.** `inventory.collect` is registered
(`handler.py:53`), directly dispatchable (`direct_dispatch.py:78`), gated
`jobs.dispatch`, and in the AI approved-action set. No worker sweep in
`app/worker.py` dispatches it and the agent's scheduler wheel registers no
inventory task. `DEFAULT_SWEEP_POLICY = {"interval_hours": 24}` is seeded into
`policies` and read by nothing. **So "it takes effect on the next ingest" means
"whenever a human next presses collect", which for an unattended fleet is
never.** This is the fact that makes §5.3 mandatory rather than a nicety.

**Referential shape of a group id.** Nine foreign keys point at
`device_groups.id`, all with the default `NO ACTION` — no `ON DELETE` clause
appears in any migration: `device_groups.parent_group_id` (self), `devices`,
`credentials`, `policies`, `user_roles` (all `0001`), `remote_policies`
(`0010`), `patch_policies` (`0011`), `backup_policies` (`0014`),
`health_profile_assignment` (`0042`). Four more references carry a group id in
a **bare uuid column or a document**, with no FK at all: `deployments.target_id`
and `script_runs.target_id` (`deploy/models.py:69,128`), `doc_links.target_id`
under `target_type = "device_group"` (`models/documentation.py:63`), and
`bcp_plan.scope` jsonb entries `{"kind": "group", "id": ...}`. Plus the audit
log, which is immutable by construction and carries group ids in metadata
forever (`user_admin/service.py:676` writes `device_group_id` into
`user.role_assigned`).

**So a hard `DELETE` has two failure modes and no success mode:** it raises a
foreign-key violation where an FK exists, and it dangles silently where one does
not. §7 follows from that.

**Health is the only module whose group binding is pushed.** `patch/policy.py`,
`backup/policy.py` and `remote/policy.py` all resolve the policy **at point of
use**, reading `Device.device_group_id` at dispatch time, so a membership change
needs no push. `health` is different: `resolve_profile_for_device` walks the
parent chain, and `sweep.run_policy_reconcile` pushes `health.policy.apply`
wherever a device's acknowledged `policy_version` is behind its effective
profile. Five health routes already call it after a write. Any group write that
can move a device's effective health profile must do the same, and nothing else
needs a reconcile.

**`MAX_GROUP_DEPTH = 16`** lives in `health/resolution.py` and exists because
the kernel schema permits a cycle. Past the bound the walk is truncated and the
device silently falls through to the org default, with a `logger.error`.

**The list route** (`787461f`) is `GET /device-groups`, gated `devices.read`,
ordered by `name` *because* that is precedence, returning `id`, `name`,
`parent_group_id`, `description`, `dynamic` (`rules is not None`) and a
**direct-membership** `device_count`, plus `total` and `ungrouped_device_count`
on the envelope. `rules` is deliberately absent: as `dict[str, Any]` it would
make the response an open key space.

---

## 3. What this design does not do

- It does not change what a group *means* to `deploy`, `patch`, `backup`,
  `remote` or `bcp`. The only consumer edit anywhere in this design is
  `user_admin::_visible_group` (`service.py:277`), the codebase's one existing
  "does this group resolve" check, which additionally refuses an archived
  group. No consumer's semantics move.
- It does not add a second membership mechanism. `devices.device_group_id`
  stays the one column, and a device is in at most one group.
- It does not make group targeting descend the tree. `parent_group_id` remains
  a health-inheritance edge and a display edge, exactly as `787461f` recorded.
- It does not touch the wire protocol or the agent. Nothing in this design
  reaches an endpoint except through `health.policy.apply` jobs the health
  module already mints.

---

## 4. The routes

```
POST   /api/v1/device-groups                      create
GET    /api/v1/device-groups/{group_id}           one group, WITH its rules document
PATCH  /api/v1/device-groups/{group_id}           name / description / parent / rules
DELETE /api/v1/device-groups/{group_id}           archive (§7)
POST   /api/v1/device-groups/{group_id}/unarchive reverse an archive (§7.3)
POST   /api/v1/device-groups/{group_id}/members   add devices        (see §5.2)
DELETE /api/v1/device-groups/{group_id}/members/{device_id}   remove one
POST   /api/v1/device-groups/preview              dry-run a rules document (§5.3)
```

`POST /{group_id}/members` takes **at most 500 device ids per request** and
answers per-id results rather than failing the batch, in the shape
`POST /devices/candidates/promote` already uses: *"one stale entry in a
selection must not silently discard the rest"*. The bound exists so one audit
event's `device_ids` list stays a readable record rather than an unbounded
blob.

`GET /device-groups` (the shipped route) gains one additive query parameter,
`include_archived` (default `false`), and excludes archived rows by default.
That is a change to a shipped response's *contents*, not its shape; it is
called out here so it is reviewed rather than discovered.

**Membership is written on the group, not on the device**, and that is a
choice. `PATCH /devices/{id}` gaining a `device_group_id` field was the other
option and it is a worse first move for two reasons: it is one device per
request on a screen whose whole job is bulk, and it is gated `assets.manage`
(`devices.py:582`), a code `technician` holds — so the device-side field would
hand fleet re-segmentation to a role that §8 deliberately does not give it. The
device-side field is a reasonable follow-on **once it carries the same
`devices.manage` gate**, and it is out of scope here.

`GET /device-groups/{group_id}` returns the rules document, and this does not
contradict `787461f`. That commit excluded `rules` because a `dict[str, Any]`
field makes a response an open key space. §5.1 gives the document a closed
Pydantic model; a typed model on a single-row detail route has neither problem.
The paged list route still does not carry it.

---

## 5. Creation

### 5.1 The rules document, and what validates it

The stored shape does not change — the evaluator is not rewritten and no
existing document is migrated. What changes is that the document now passes
through a **closed** Pydantic model on write:

```python
Op = Literal["eq", "ne", "contains", "gt", "lt"]

class RuleCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fact: str
    op: Op
    value: str | int | float | bool

class GroupRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    all: list[RuleCondition] = Field(min_length=1)
```

with three validators, each of which exists because the evaluator has a matching
silent failure:

1. **`fact` must resolve.** Either a member of the flat namespace
   (`hostname`, `os_family`, `os_version`, `architecture`, `ram_bytes`,
   `serial`), or prefixed `facts.` or `software:` with a non-empty remainder.
   Anything else is a `422`. This is the `ne`-on-a-typo case from §2: without
   it, `{"fact": "os_familly", "op": "ne", "value": "windows"}` claims every
   device in the org and reads like a reasonable rule.
2. **`gt` and `lt` require a value `float()` accepts.** `_condition_holds` calls
   `float(expected)` and raises `ValueError` on anything else, which the ingest
   path catches and logs. A rule that can never be evaluated is an authoring
   error and belongs at the point of authorship.
3. **`contains` on `ram_bytes` and the other numeric-only combinations are
   permitted**, because `str(value)` is well defined and the operator may
   genuinely want it. *A rule that matches nothing is not an error. A rule that
   cannot be evaluated is.* That is the line, and these three validators are
   exactly it.

**The validator is derived from the evaluator, and a test asserts it.**
`_condition_holds` is an `if`-chain today; the ops move into one tuple in
`rules.py` that both the chain and this `Literal` are built from, and a test
asserts the two sets are equal in both directions.
Two independently maintained lists is how a rule the API accepts becomes a rule
the ingest skips, and the only evidence would be a `logger.warning` nobody
reads.

### 5.2 Static, dynamic, or both

`dynamic` on the list response means, today and after this change,
`rules is not None`. Nothing about that field's meaning moves.

What must be settled is whether a rule-bearing group may also hold hand-placed
members. It is **OPEN 3** in §10; the recommendation is **no, enforced**:

- `POST /{group_id}/members` on a rule-bearing group is a `409` naming the
  reason.
- `PATCH` adding rules to a group that holds hand-placed members is a `409`
  listing the count, with the remedy stated (empty it, or create a new group).
- The list route's `dynamic` flag therefore means what its docstring already
  claims — *"a console offering to hand-place a device into one of these would
  be offering an edit the next ingest reverts"* — instead of being a warning
  the API declines to enforce.

### 5.3 When a rule-bearing group takes effect

Three candidates, and their worst cases are not comparable:

| when | worst case |
|---|---|
| **immediately, inside the request** | one `POST` re-reads every device's latest snapshot, re-evaluates, and moves an arbitrary number of machines onto different patch/backup/remote policy — inside one HTTP transaction, with an HTTP timeout and no preview. On a 5,000-device org this is a fleet-wide policy change delivered by a form submit. |
| **on the next ingest** | §2: **nothing schedules an ingest.** For an unattended fleet the answer is *never*, and the console shows a group whose `device_count` is 0 with no explanation. This is the status quo and it is not a design. |
| **a background re-evaluation** | a window — bounded by the sweep interval — in which the console shows a membership that is one write behind. Every consumer resolves policy at use, so the window is a display lag, not a wrong action, with the single exception of health, which is reconciled by the sweep itself. |

**Decision: a background re-evaluation, with a synchronous preview.** Both
halves are load-bearing.

- **`POST /device-groups/preview`** takes a candidate rules document (and,
  optionally, the id of the group it is for) and returns

  ```json
  {"matched": 37, "devices_without_inventory": 18, "sample_truncated": true,
   "sample": [{"device_id": "…", "hostname": "…",
               "current_group_id": "…", "current_group_name": "…"}]}
  ```

  — the sample capped at 25 devices, the two counts unbounded and exact. It
  writes nothing. It is what the console calls on every settled edit of the
  rules builder, and it is what makes a create or a rules change a decision
  instead of a gamble. It is registered **before** any future
  `POST /device-groups/{group_id}` route, because FastAPI matches
  first-registered-wins and the shadowing hazard is the one
  `app/api/v1/device_groups.py`'s own header already records.
- **`device_group_reevaluate`** is a new `app/worker.py` beat entry at 300s,
  in the shape `provision-enroll-dispatch` already has: enumerate orgs on the
  admin engine, then **for each org open an `org_session`**, short-circuit
  immediately when the org holds no rule-bearing group (one `COUNT`, which is
  every org on a deployment that uses only static groups), and otherwise walk
  the org's active devices. For each it reads the **latest
  `InventorySnapshot`**, rebuilds `InventoryDocument(**snapshot.raw)` — the
  ingest writes `doc.model_dump(mode="json")`, so this round-trips — and calls
  the existing `evaluate_rules`.

  **The write route does not enqueue the sweep, and that is deliberate.** No
  route in this codebase enqueues a Celery task: `grep` for `.delay(` and
  `apply_async` across `app/` returns nothing, and everything is beat-driven.
  Introducing request→broker enqueueing to shave a bounded five minutes off a
  membership update is a larger change, with its own failure modes, than the
  feature it would serve. **The lag is therefore up to 300 seconds and the
  console says so** — the group page states when membership was last
  re-evaluated, in the shape `15ed5de` established for `last_scan_at`: a
  timestamp, never alone.

Three consequences, stated rather than discovered:

- **A device with no snapshot is never claimed by a rule.** A manually-created
  asset, a discovered switch, an enrolled machine that has not yet collected —
  none of them has a document, so the evaluator has nothing to evaluate. The
  console must say so: the preview reports `devices_without_inventory` beside
  its match count, and the group detail page shows it. Anything else lets an
  operator write a rule for a fleet and receive half of it with no explanation.
- **The sweep runs `org_session` per org, never the admin engine.** §2 measured
  that `evaluate_rules` carries no `org_id` predicate; the sweep must both run
  scoped *and* the predicate must be added to `evaluate_rules` (§8).
- **The sweep calls `run_policy_reconcile` after it moves anything**, for the
  same reason five health routes already do: a device that changed group may
  resolve to a different health profile, and health is the only module that
  pushes.

### 5.4 Creating a group with no rules

The common case, and it is synchronous and dull: insert, audit, return. Name is
unique per org (`409` on collision, not a 500 out of `IntegrityError`), parent
must resolve in this org (§6.3), rules absent.

---

## 6. Update

### 6.1 Rename, and the precedence problem

**The fact.** `rules.py:83-84` orders rule-bearing groups by `name` and the first
match claims the device. So the alphabet is the precedence list. Renaming
`z-fallback` to `a-fallback` promotes it above every other rule group, and every
device that matches it moves — from whatever group held it, onto a different
patch policy, a different backup policy, a different remote policy, and possibly
a different health profile. No rule was edited. Nothing on screen said so.

Worth stating precisely, because it bounds the blame: **this only bites
rule-bearing groups.** A static group's name is a label; renaming it moves
nothing, because static membership is stored in `devices.device_group_id` and
read by id everywhere. Health inheritance keys on ids too. The rename hazard is
exactly the dynamic-membership evaluator and nothing else.

**A second, smaller fact.** `ORDER BY name` is evaluated in the database's
collation. `deploy/` sets none, so it is the image default, and a console that
sorts client-side with `localeCompare` can disagree with the evaluator about
which rule wins. The console must render the order the server returned, never
re-sort. (`787461f` already ordered the list route for this reason; this is the
same requirement pointed at the frontend.)

**The recommendation is to remove the coupling rather than warn about it** —
`precedence smallint NOT NULL DEFAULT 0`, ordered `(precedence, name)`, name
demoted to a label. `GET /device-groups` moves to the same order in the same
change, because `787461f` ordered it by name *for this reason* — a list that
kept the alphabet would start answering "which rule wins" differently from the
evaluator that decides it, which is the defect that commit's ordering exists to
prevent. This is **OPEN 1** in §10, and the argument for taking it *now* is
that the migration is free exactly once: no code path creates a group (§1), so
a deployed database holds only groups someone inserted by hand with SQL. Every
later moment costs a re-evaluation of a live fleet.

**If OPEN 1 is taken, rename is: accept, and audit.** A label change that moves
nothing needs no preview and no confirmation.

**If OPEN 1 is declined, rename is: preview, then confirm.** The alternatives
were considered and rejected:

| option | why not |
|---|---|
| refuse to rename a rule-bearing group | the name is the only human-readable handle a group has; a group you cannot rename is a group you name wrong once |
| warn in the response body | a warning after the write has happened is a description of damage |
| accept silently and audit | this is the current behaviour of a route that does not exist yet. **Choosing it is choosing it, not inheriting it.** The audit entry would be the only record that a fleet re-segmented, discovered afterwards |
| **preview, then confirm** | *recommended in this branch*: `PATCH` with a name change on a rule-bearing group returns `409` with the preview body (`moves`, `sample`) unless the request carries `confirm_moves: true`. The console shows "12 devices will move" and the button says so |

### 6.2 Changing the rules document — the same answer, deliberately

A rules edit moves devices for the same reason a rename does, and it is not a
smaller act: an edit can empty a group entirely. It takes the identical path —
preview, `confirm_moves: true` when the preview is non-empty, the same audit
event shape, the same background re-evaluation. **This is the same answer as
§6.1 and it is the same answer under either branch of OPEN 1**, because an
explicit `precedence` column does nothing to make a rules edit safe. Saying so
explicitly matters: the two are one code path, and a design that answered them
differently would have two.

### 6.3 Reparenting: cycles, orphans, depth

`parent_group_id` has no constraint of any kind. Three checks, all at write
time, all in the service:

- **Self-parent** — `422`. Trivially, and it is the one a UI drag-and-drop
  produces first.
- **Cycle** — walk the proposed parent's ancestor chain; if this group appears,
  `422` naming the two groups. `health/resolution.py` already survives a cycle
  (bounded walk, `logger.error`, fall through to the org default) — but
  surviving it means every device beneath the cycle silently gets the wrong
  profile. Refusing at write time is the fix; the health bound stays as the
  backstop it is.
- **Depth** — the health walk truncates at `MAX_GROUP_DEPTH = 16`. A reparent
  must be refused when `depth(new_parent) + 1 + subtree_depth(this_group) > 16`,
  because moving a shallow subtree under a deep parent pushes its *leaves* past
  the bound even though the group itself is fine. The bound is defined once in
  the group service and **a test asserts it equals
  `health.resolution.MAX_GROUP_DEPTH`** — a test rather than an import, because
  the kernel must not depend on a module (CLAUDE.md: modules register into the
  kernel, they do not fork it), and two copies of a constant is two chances to
  disagree.
- **Cross-org parent** — the self-FK is bare, exactly like
  `devices.device_group_id`, so the schema permits naming a neighbour's group as
  a parent. Refused in application code, in the shape
  `health/profiles.py::_assert_target_in_org` already uses. §8 says how that
  test must be written so it can fail.

**Orphans cannot occur**, because §7 refuses to archive a group that has
children. There is no path that leaves a `parent_group_id` pointing at nothing.

Reparenting moves health resolution for every device beneath the moved subtree,
so the write calls `run_policy_reconcile` after it, in the shape
`health/api.py::_reconcile` uses: inside the tenant transaction, published after
commit.

---

## 7. Deletion

### 7.1 Soft, not hard, and the argument is `15ed5de`'s

`15ed5de` soft-revoked patch approvals *"precisely so the trail holds"*.
`devices.py:427`'s suggestion-resolve says the same thing about device rows: a
delete *"would take its job history, its documentation links and its audit
trail's target with it; retirement leaves all of that answerable"*. Both
arguments transfer intact, and a group has more inbound references than either:

- The audit log is immutable and hash-chained. A `user.role_assigned` event
  carrying `device_group_id` must still resolve to a name in five years.
- `deployments.target_id` and `script_runs.target_id` name a group by bare uuid
  with no FK. A completed deployment against a hard-deleted group would render
  as a uuid nothing can name — the B44/B64 defect the UX programme spent a
  cycle closing.
- Nine FKs are `NO ACTION`, so the hard delete does not even succeed while any
  of them holds. It raises, and the route would answer 500 unless it duplicated
  the check anyway.

**So: `archived_at timestamptz NULL`.** `DELETE /device-groups/{id}` sets it.
Archived groups are excluded from `GET /device-groups` by default, refused by
`user_admin::_visible_group` as a new role scope, and remain readable by id
forever.

**What is deliberately NOT built here**, because it would touch six modules:
deploy, patch, backup, remote, health and bcp each resolve a group id by
matching on it, and none of them will learn about `archived_at` in this design.
They do not need to. §7.2 refuses the archive while the group holds devices,
child groups, policies, health assignments, credentials or role grants — so an
archived group is empty and unreferenced by construction, and a deployment
someone aims at one by typing its id hits zero devices rather than the wrong
ones. Per-module refusals are honest follow-on work and are recorded in §12
rather than assumed.

**One migration detail that is easy to miss.** `UniqueConstraint("org_id",
"name")` means an archived group holds its name against every future group. The
constraint becomes a **partial unique index** `WHERE archived_at IS NULL`. The
alternative — renaming on archive — rewrites a name the audit log already
references, which is the thing archiving exists to avoid.

### 7.2 What happens to members: refuse, do not silently re-home

**Recommendation: `409` while the group has direct members or child groups**,
with a body enumerating every live reference:

```json
{"detail": "this group is still in use",
 "blocked_by": {"devices": 12, "child_groups": 1, "patch_policies": 1,
                "backup_policies": 0, "remote_policies": 0,
                "health_assignments": 1, "credentials": 0, "role_grants": 2}}
```

The rejected alternative is *"archive it and let the members fall back to
`NULL`"*, and it is rejected because `NULL` is not an absence. §1 and §2: every
policy resolver reads `device_group_id IS NULL` as the **org default**. So a
one-click archive of a 300-device group is a 300-device patch-policy change, a
backup-policy change and a remote-policy change, delivered by a button labelled
Delete. If the operator wants that, they can have it — by emptying the group
first, in an act that says what it is and produces its own audit event and its
own count on screen.

This is **OPEN 2** in §10, because it is a workflow the operator lives with
daily and reasonable people prefer the one-step version with a preview.

`role_grants` in that list is the one worth naming out loud: a `user_roles` row
scoped to a group is RBAC. Archiving out from under it would leave a grant
scoped to a group no surface can show, which is a permission nobody can audit.

**Emptying a RULE-bearing group needs one extra step, and it is not the one it
looks like.** §5.2 refuses `DELETE /{id}/members/{device_id}` on a rule-bearing
group, so the members must first stop being rule-claimed: `PATCH` the rules to
`null`. **That moves nobody** — `evaluate_rules` only ever *assigns* on a match
and never unassigns (§2), so the devices stay exactly where they are and the
group simply becomes static, holding the same members. They can then be released
explicitly, one act at a time, each visible. Without this paragraph the two
rules in this spec would deadlock a rule-bearing group into being unarchivable,
which is the kind of contradiction a spec exists to catch.

**No `force` parameter.** A flag that turns a refusal into the thing the
refusal exists to prevent is the refusal not existing.

### 7.3 Unarchive

`POST /device-groups/{id}/unarchive`, same permission, its own audit event. The
reason is `bd04957`'s, quoted in the UX closeout for exactly this shape: a
one-way state change with no way back is the defect (B76), and an undo is
cheaper to build now than to retrofit. It refuses when a *live* group in the org
already holds the name — which the partial index makes possible — with a message
saying so.

---

## 8. RBAC, tenancy, audit

### 8.1 `devices.manage`, and no new code

`e25bbe7` made adding a permission code a deliberate act. This one needs none:

- `devices.manage` exists in `PERMISSIONS` (`app/core/rbac.py:22`) and in
  migration `0003`. Its description is *"Create, edit, group, and decommission
  devices"* — **"group" is already the word in it**.
- `0003` seeds it to `super-admin` and `org-admin` and to nobody else.
  `technician`, `auditor` and `read-only` hold `devices.read` and not this.
- It gates no route today, so nothing anywhere changes gate. No migration, no
  decision about which seeded roles hold it, no new row in the matrix.

**Every write route in §4 takes `devices.manage`. Every read stays
`devices.read`,** including `GET /device-groups/{id}` and its rules document —
a rules document names fact paths and values, not device data, and the caller
entitled to see the fleet's shape is entitled to see how it is carved.
`/preview` is the exception among reads and takes **`devices.manage`**: it
answers "which machines would this claim", which is the authoring act, and it is
the one read that enumerates the fleet against an arbitrary caller-supplied
predicate.

**Why `technician` deliberately does not get it**, since `0062` gave that role
`assets.manage` on the reasoning that *"recording the switch you are standing in
front of is the ordinary work of the role"*: creating or renaming a rule-bearing
group re-binds patch, backup and remote policy for machines the technician is
not standing in front of, and a group is an RBAC scope. That is a different
class of act from writing an asset row, and `0003`'s existing seeding already
draws the line in the right place. Leaving it there is also the answer with no
migration in it.

**Archive is not separately gated.** Invariant 1b(f) gates a destructive subset
separately because remote lock and erase are irreversible acts on a device. An
archive that is refused while occupied and reversed by `/unarchive` is neither.

### 8.2 Audit — every write, and what a rename records

All events go through `audit_service.record` inside the tenant transaction.
The six operator-driven ones are `actor_type="user"`,
`target_type="device_group"`, `target_id=<group id>`; the sweep's is
`actor_type="system"`, because no user pressed anything at the moment it ran:

| action | metadata |
|---|---|
| `device_group.created` | `name`, `parent_group_id`, `dynamic`, and the full `rules` document when present |
| `device_group.updated` | `changed`: `{field: {"from": …, "to": …}}` for each of `name`, `description`, `parent_group_id`, `rules`; plus `previewed_moves` when the write carried `confirm_moves` |
| `device_group.members_added` / `.members_removed` | `device_ids` (bounded at 500 by §4) |
| `device_group.archived` / `.unarchived` | `name`, and `member_count` (0 by §7.2) |
| `device_group.membership_reevaluated` | written by the **sweep**, once per run, `actor_type="system"`, `target_type="organization"`: `groups_evaluated`, `devices_evaluated`, `devices_moved`, `devices_without_inventory`, and `moves` — `[{device_id, from_group_id, to_group_id}]` truncated at 100 with an explicit `moves_truncated: true` and the exact `devices_moved` count beside it |

**A rename records what moved, and it records it in two places, on purpose.**
The write event carries the count the operator *confirmed*; the sweep event
carries what actually moved. They can differ — the fleet changes between the
preview and the sweep — and a log that recorded only one of them would be
answering a question nobody asked. This is also why the sweep does not write one
event per device: `audit_service.record` takes a per-org
`pg_advisory_xact_lock` and reads the chain head on every append, so 5,000
devices would be 5,000 serialized head reads inside one transaction. The
truncation is stated in the event rather than hidden, because an audit entry
that implies completeness it does not have is worse than one that admits its
bound.

### 8.3 Tenancy — and the tests that can actually fail

`device_groups` already carries `FORCE ROW LEVEL SECURITY` and the
`tenant_isolation` policy (`0002`), and `archived_at` — plus
`precedence`, if OPEN 1 lands on (a) — are columns on an existing table, so
**no new RLS policy is needed**. Which is
exactly the trap `docs/superpowers/audits/2026-08-21-tenancy-test-efficacy.md`
§0 documents: everything served through `org_session` runs as `app_user`
(NOBYPASSRLS), so deleting the application's `WHERE org_id` turns **no route
test red**. `787461f`'s author proved it by doing it.

Requirements, not suggestions:

1. Every service function added here carries an explicit
   `DeviceGroup.org_id == org_id` predicate, and so do the parent-resolution,
   member-resolution and blocked-by counts.
2. **`evaluate_rules` gains the `org_id` predicate it does not have**
   (`rules.py:83`), and takes `org_id` as an argument rather than reading it off
   the device. It is safe today only because its one caller happens to run
   inside `org_session`; this design adds a second caller, in the worker, where
   four sibling sweeps hold an admin engine.
3. Route-level tenancy tests use **idiom 3** from that audit — repoint
   `env.app.state.app_engine = env.admin_engine` and drive the real route — so
   the application predicate is the only thing between the caller and a
   neighbour's row. A test that drives `org_session` and asserts invisibility is
   testing RLS and must be named `..._is_hidden_by_rls`, per the audit's naming
   rule.
4. The cross-org fixture includes the row the schema does not forbid, as
   `test_device_groups_api.py:393` already does: another org's device naming
   this org's group, and this org's group named as another org's parent.

---

## 9. The console

The surface is a **Groups** entry under **Fleet** in `src/shell/nav.ts`, gated
`devices.read`, beside Devices & Inventory.

**The list is a tree, and the first row is not a group.** `ungrouped_device_count`
renders as a fixed top row — "No group · org default · N devices" — because that
is the scope every policy editor already offers and the audit's finding was that
the console could name it and not size it. Below it, groups nested by
`parent_group_id`, each with its direct `device_count`, a `dynamic` badge, and —
where §10 OPEN 1 lands on an explicit column — its precedence position with
up/down controls. **Order comes from the server** (§6.1); the tree is built by
grouping the server's sequence under parents, never by re-sorting.

**The rules builder is rows, not a textarea.** Recommendation 4 of the UX
closeout was *"One error surface: no raw JSON in the UI"* and recommendation 6
was *"`EntityPicker`: kill the hand-typed UUID"*, across ~60 adoption sites. A
JSON textarea for `DeviceGroup.rules` would reintroduce both in one control.
Each condition is three inputs:

- **fact** — a `<select>` over the flat namespace, plus two entries that reveal
  a second labelled input: "Custom fact (`facts.…`)" and "Installed software
  (`software:…`)". The evaluator's namespace is closed (§5.1), so the select can
  be exhaustive and a typo becomes unreachable rather than a 422.
- **op** — a `<select>` over the five ops, labelled in words ("is", "is not",
  "contains", "greater than", "less than").
- **value** — `type="number"` when the op is `gt`/`lt`, a checkbox when the
  chosen fact is a known boolean (`facts.domain_joined` and friends), text
  otherwise.

Beneath it, live from `POST /device-groups/preview`: **"Matches 37 of 412
devices. 18 devices have no inventory yet and cannot be matched."** The second
sentence is not decoration — §5.3 says an un-collected device is unmatchable,
and an operator who is not told that will conclude their rule is wrong.

**Parent is an `EntityPicker`** (`components/picker.tsx`) over the group list,
never a typed UUID — the control that exists precisely because five fields once
took one.

**Rename and rules edits show what moves before they happen.** When the preview
is non-empty the save button reads "Save — 12 devices will move" and opens a
`ConfirmDialog` (`components/confirm.tsx`) listing a sample with each device's
current group, in the shape B76's remediation established. Under OPEN 1's
recommendation a plain rename skips this entirely, because it moves nothing.

**Archive uses `ConfirmDialog` and renders the refusal.** When the server
answers `409` with `blocked_by`, the dialog lists each blocker as a link to the
thing holding it — 12 devices to the device list filtered by this group, a patch
policy to the patch policy editor, a role grant to the user's roles page. A
refusal the operator cannot act on is a dead end; this one is a to-do list.

**Live counts, and what the console cannot be told.** `queue_ui_event`
(`gateway/dispatch.py:77`) stashes events on `session.info` and is drained by
the **gateway's** message handler; `app/worker.py` never calls it and there is
no worker→UI event path in this codebase. So the sweep publishes nothing and
the group page cannot be pushed to. It refetches on its own writes (TanStack
Query invalidation) and shows the last re-evaluation timestamp beside the
counts, which is what makes a count that is up to 300 seconds stale legible
rather than wrong. Building a worker→UI channel is a kernel change and is not
smuggled in here.

**A documentation correction ships with this.** `docs/modules/inventory/spec.md`
§7's sentence about manual placements (§2) is false against `rules.py` and would
mislead the next reader into believing a hand-placed device is safe inside a
rule-bearing group. It is corrected in the cycle that makes the distinction
enforceable (§5.2).

---

## 10. DECIDED — the three questions this section put to the operator

> **All three were answered on 2026-08-22, and each took the recommendation.**
> The section is kept as written, because the reasoning is the record of why
> the answer is what it is; what follows is what was chosen and what it binds.
>
> **10.1 Precedence — an explicit `precedence smallint` column.** Ordering
> becomes `(precedence, name)`, name is demoted to a label, and
> `GET /device-groups` moves to the same order. Rename is then an audited
> relabelling that moves no device. The timing argument is the one that
> settled it: no code path creates a group today, so this migration is free
> exactly once and costs a live fleet re-evaluation at every later moment.
> This lands in **cycle 1**, before anything can create a rule-bearing group,
> or the free window closes.
>
> **10.2 Archiving an occupied group — refuse, and enumerate.** A `409` naming
> every live reference. Re-homing to `NULL` was rejected on the spec's own
> argument: `NULL` is the org default that every policy resolver reads, so a
> one-click archive of 300 devices would be a 300-device patch, backup and
> remote policy change wearing a Delete button.
>
> **10.3 Membership — static XOR dynamic, enforced.** A group is hand-curated
> or rule-driven, never both. This ends the behaviour where the ingest loop
> silently reverts an operator's placement on the next collection, and it
> makes the console honest: a rule-driven group offers no "add device" control
> at all. It also settles the documentation defect §9 records — `docs/modules/
> inventory/spec.md` §7 claims rule groups never unassign manual placements,
> which the loop does not honour; with this decision the claim becomes true by
> construction rather than by correction.

### The original three questions, as put


### 10.1 Precedence: an explicit column, or the alphabet

Dynamic-membership precedence is currently the group's name (§6.1). The
mechanism either way is `ORDER BY` in one statement; the question is what it
orders on.

| option | consequence |
|---|---|
| **(a) add `precedence smallint NOT NULL DEFAULT 0`, order `(precedence, name)`** | *recommended*. Rename becomes a label change that moves nothing. Precedence becomes a thing the console can show and drag. Costs a column, a migration, a one-line change to `rules.py`, and a decision the operator makes once |
| (b) keep name ordering, and make rename go through preview-and-confirm (§6.1) | no migration, no change to a shipped evaluator. Leaves a rename permanently able to re-segment a fleet, mitigated by a confirmation the operator can click through |
| (c) keep name ordering, refuse to rename a rule-bearing group | cheapest, and it makes the name unfixable, which is how groups end up called `servers-2-FINAL` |

**Recommendation: (a), and the timing is the argument.** No code path creates
a group (§1), so any group in a deployed database was inserted by hand with SQL
— which on this project's own on-prem install means there are none. Changing
what decides precedence therefore costs nothing today, and costs a live fleet
re-evaluation at every later moment. If (b) is preferred, §6.1's
preview-and-confirm branch is the design and nothing else in this document
changes. If (c) is preferred, the same branch applies with the `409` made
unconditional. §6.2 — the rules edit — is unaffected by all three.

### 10.2 Archiving an occupied group: refuse, or re-home with a preview

| option | consequence |
|---|---|
| **(a) `409` listing every live reference; the operator empties it first** | *recommended*. A fleet-wide policy change is never a side effect of a Delete button. Two steps, each with its own audit event and its own visible count |
| (b) archive and re-home members to `NULL`, behind a confirmation naming the count | one step. But `NULL` is the org default (§1), so the confirmation is carrying "and 300 machines change patch, backup and remote policy" — and the org-default policy set is on a different screen |
| (c) archive and re-home to the group's **parent** | reads tidiest and is the most surprising: parent membership is not inherited by deploy, patch, backup or remote (only health walks the tree), so "moved to the parent" means something different in five modules |

**Recommendation: (a).** (b) is defensible if the operator's real workflow is
"make a group, try it, throw it away" — say so, and it becomes the design with
the preview from §5.3 wired to the archive dialog instead. (c) should not be
chosen; it encodes a tree semantic only one consumer has.

### 10.3 May a rule-bearing group hold hand-placed members?

Today it can, and the next ingest silently evicts them (§2), which is the one
thing `787461f` says a console must not get wrong.

| option | consequence |
|---|---|
| **(a) static XOR dynamic, enforced (§5.2)** | *recommended*. `dynamic` on the list response becomes a fact the API enforces rather than a warning it declines to act on. Costs the operator a second group when they want "everything matching X, plus these three" |
| (b) allow both, rules additive rather than authoritative | the friendlier model, and it is **not** what the evaluator does: `evaluate_rules` *moves* a device, it never adds one, and membership is a single column. Building (b) means a membership join table and rewriting every consumer's `device_group_id == target_id`. That is a different spec |
| (c) allow both, ingest wins — the status quo | an operator's edit is reverted by a background process with no message. The support ticket says "it forgot my change" |

**Recommendation: (a).** (b) is the better long-run model and should be argued
on its own, as a change to how membership is stored, not smuggled in here.

---

## 11. Verification

Every item is a test that must be able to fail; where the audit's engine trap
applies it is named.

**Creation**

1. `POST` creates a group and returns it; a duplicate name in the same org is a
   `409`, not a 500 out of `IntegrityError`.
2. A rules document with an unknown bare fact path is a `422` — and the test
   that matters is `{"fact": "os_familly", "op": "ne", …}`, because under the
   current evaluator that claims the whole org.
3. `{"op": "gt", "value": "soon"}` is a `422`; `{"all": []}` is a `422`; an
   unknown key anywhere in the document is a `422`.
4. The validator's op set equals the set `_condition_holds` dispatches on, in
   both directions. Adding an op to either alone turns this red.
5. `/preview` writes nothing (asserted by re-reading every device's
   `device_group_id` after), reports an exact count with a truncated sample, and
   reports `devices_without_inventory` for a device with no snapshot.

**Re-evaluation**

6. The sweep replays the latest `InventorySnapshot.raw` and produces the same
   assignment the ingest path produces for the same document.
7. A device with no snapshot is untouched and counted in
   `devices_without_inventory`.
8. **Tenancy, on the admin engine:** two orgs each with a rule-bearing group
   whose rule matches the other's devices; the sweep moves only its own org's.
   Deleting the `org_id` predicate added to `evaluate_rules` turns this red.
   *This test does not exist today and the predicate does not exist today.*
9. The sweep calls `run_policy_reconcile` when it moved a device, and does not
   when it moved none.

**Update**

10. Under OPEN 1(a): renaming a rule-bearing group moves no device — asserted
    by re-running the evaluator and comparing assignments. Under OPEN 1(b):
    a rename whose preview is non-empty is a `409` without `confirm_moves`, and
    the same request with it succeeds and audits `previewed_moves`.
11. A rules edit follows the identical path — the test is written against the
    same helper as 10, so the two cannot drift apart.
12. Self-parent, a two-group cycle, and a three-group cycle are each `422`.
13. A reparent that would push a leaf past the depth bound is `422`; the bound
    equals `health.resolution.MAX_GROUP_DEPTH` (asserted directly).
14. A parent id belonging to another org is refused — **on the admin engine**,
    driving the real route, or the test asserts nothing.
15. Reparenting triggers a reconcile; a description-only edit does not.

**Deletion**

16. Archiving a group holding devices is `409` with `blocked_by.devices == N`;
    likewise for a child group, a patch policy, a health assignment, a
    credential and a `user_roles` grant — six cases, each proven to block on its
    own by removing only that reference and watching the archive succeed.
17. An archived group is absent from `GET /device-groups`, present with
    `include_archived=true`, still readable by id, and refused by
    `user_admin::_visible_group` as a new role scope — the one consumer-side
    refusal §7.1 builds. The modules that are deliberately *not* taught about
    `archived_at` get a test of their own: a deployment aimed at an archived
    group resolves to **zero** devices, which is what makes leaving them alone
    safe.
18. A new group may take an archived group's name (the partial unique index);
    unarchiving into a live name collision is a `409`.
19. A completed deployment whose `target_id` names an archived group still
    resolves to that group's name — the B44/B64 property, asserted rather than
    assumed.

**RBAC**

20. Each write route: `org-admin` 200, a caller holding `devices.read` alone
    403, a caller holding `assets.manage` alone 403 (the technician's shape,
    which is the negative control §8.1 turns on), no permission 403.
21. `/preview` requires `devices.manage`, not `devices.read`.

**The two census guards**

22. `tests/test_ai_tool_surface.py` measures every GET route under `/api/v1`
    and pins the totals; `GET /device-groups/{group_id}` is a new GET route, so
    the census moves by one and must be **re-measured against the tree**, not
    computed by arithmetic on the last entry. Whether the detail route joins
    `PENDING_TOOL_REVIEW` or the registry is a decision about the AI persona's
    reach — the same call `787461f` made for the list route, and it should be
    made the same way: a group's rules document names the fleet's segmentation,
    which is reconnaissance, so `PENDING_TOOL_REVIEW`.
23. `tests/test_route_gating.py` asserts every route under `/api/v1` is gated;
    each new route must report its permission through `route_permission`, and
    none of them belongs on that file's named-exceptions list.

**Audit**

24. Each write appends exactly one event of the stated action with the stated
    metadata, and `GET /audit/verify` still passes afterwards — the chain is
    unbroken by the new events.
25. A sweep that moves more than 100 devices writes one event whose
    `devices_moved` is exact and whose `moves_truncated` is `true`.

**Console**

26. The rules builder renders no `<textarea>` and no free-text UUID field; parent
    is an `EntityPicker`; the fact control is a `<select>` whose options are the
    closed namespace.
27. The tree renders the server's order — falsified by returning a
    deliberately non-alphabetical order from a stubbed client and asserting the
    DOM matches it rather than the alphabet.
28. The preview sentence names both numbers, including the no-inventory count.
29. Archive's `409` renders each blocker as a link, not as a JSON blob.

**Falsifiers for this design's own claims**

| claim | falsifying observation |
|---|---|
| A rename cannot silently re-segment a fleet | a device whose group changed with no confirmation and no audit entry naming the move |
| A rule the API accepts is a rule the evaluator can run | a `logger.warning` from `evaluate_rules` about a document that was written through `POST`/`PATCH` |
| Archiving never changes a device's policy | a device whose effective patch/backup/remote policy changed as a result of an archive |
| Group writes are tenant-scoped in application code | deleting any `org_id` predicate added here and seeing the suite stay green |
| The console never shows a raw group id or a JSON rules blob | either one on screen |

**Idle budget.** Nothing in this design runs on the agent, adds a capability, or
changes a wire schema. The budget is untouched, stated because CLAUDE.md
requires the question to be answered rather than skipped.

---

## 12. Known limits, stated now

- **Membership stays one column.** A device is in at most one group. "This
  machine is in Finance *and* Laptops" is not expressible, and OPEN 3(b) is
  where that conversation belongs.
- **A device with no inventory can never be claimed by a rule**, and no
  design here changes that — the evaluator reads documents (§2).
- **Re-evaluation is eventually consistent** within the sweep interval. Every
  consumer except health resolves at use, so the window is a display lag; health
  is reconciled by the sweep itself.
- **`precedence` (if adopted) does not make rules deterministic across
  concurrent edits.** Two operators reordering at once produce a last-write-wins
  ordering. Acceptable, and named.
- **No per-device move history is stored.** The audit log carries counts and a
  bounded sample (§8.2). A complete "why is this machine in this group" history
  would need a table of its own and is not built here.
- **A module's API still accepts an archived group id typed directly.**
  Deploy, patch, backup, remote, health and bcp are not taught about
  `archived_at` (§7.1). This is safe rather than sloppy — §7.2 guarantees an
  archived group holds no devices, no child groups, no policies, no health
  assignments, no credentials and no role grants, so the worst outcome is an
  action against an empty set. Adding the refusal per module is follow-on work
  for whoever next touches each.
- **`GET /device-groups`'s `device_count` remains direct membership** and does
  not descend the tree, for `787461f`'s reason: it sizes the blast radius of a
  group-targeted job, and deploy does not descend.

---

## 13. Cycles

**Two, not one.** The split is where the risk changes character: cycle 1 touches
no code that moves a device on its own; cycle 2 touches the evaluator.

**Cycle 1 — static groups, and the code that gates them.**
`POST`/`GET {id}`/`PATCH`/`DELETE`/`unarchive`, the two membership routes, the
`archived_at` column and its partial unique index, **the `precedence` column
and the one-line change to `rules.py`'s ordering** (§10.1, as the operator
settled it: the migration is free exactly once and the window closes when
cycle 2 ships a rules editor), the hierarchy checks (self,
cycle, depth, cross-org), the `blocked_by` refusal, `devices.manage` on every
write, all audit events except the sweep's, the tenancy tests on the admin
engine, the health reconcile after a reparent, `include_archived` on the shipped
list route, `user_admin::_visible_group` refusing archived groups, the two
census guards (§11.22-23 — `GET /device-groups/{group_id}` moves the GET route
count and must be re-measured against the tree), and the console's Groups tree
with create / rename / reparent / archive.
**No rules field is accepted in this cycle** — `POST` and `PATCH` reject it with
a message naming cycle 2. This ships the thing the audit asked for: a fleet that
can be carved up and addressed, from the console, by an operator.

**Cycle 2 — the rules document.** `GroupRules` and its three validators, the
derived-op test, `GET {id}` returning the typed document, `PATCH` accepting it,
`POST /preview`, the
`org_id` predicate on `evaluate_rules`, the worker sweep and its reconcile, the
sweep's audit event, the static-XOR-dynamic enforcement from OPEN 3, the console
rules builder and live preview, and the `docs/modules/inventory/spec.md` §7
correction.

Cycle 1 is a prerequisite for cycle 2 (the routes, the permission gate, the
audit shape). Cycle 2 is where a mistake moves machines, and it should be
reviewed as such.

### What cycle 2 built that this document did not ask for, and what it did not build

Recorded here rather than left as a diff to be reconstructed.

**Three additions, each of which follows from a sentence already in this spec
rather than from a new opinion.**

1. **The evaluator ignores ARCHIVED rule-bearing groups.** §7.1 lets deploy,
   patch, backup, remote, health and bcp stay ignorant of `archived_at` on the
   strength of one sentence — an archived group holds no devices, so an action
   aimed at one reaches zero of them. An empty rule-bearing group has no live
   references, so §7.2 permits archiving it; without this predicate the next
   sweep would repopulate it, leaving a group that is archived, occupied and
   (by §7.2) unarchivable. The spec does not state the predicate; §7.1 requires
   it.
2. **`DeviceGroup.rules` now stores SQL `NULL` rather than jsonb `'null'`, and
   the group query asks `jsonb_typeof(rules) = 'object'`.** SQLAlchemy
   serializes a Python `None` into the JSON literal `null` by default, so every
   static group cycle 1 created was written as a value for which `rules IS NOT
   NULL` is TRUE. It read back as Python `None`, so `dynamic` was always
   correct and no response was ever wrong — but `evaluate_rules` selected every
   static group as rule-bearing and logged a warning it could not run the empty
   document, which is §5.1's "`logger.warning` nobody reads" being written for
   real. It becomes load-bearing in cycle 2: §5.3's short circuit asks whether
   an org holds any rule-bearing group, and the answer would have been yes for
   every org that ever made a static one. No migration — the model fix governs
   new writes and the predicate governs the rows already written.
3. **The sweep writes no audit event for an org holding no rule-bearing
   group.** §8.2 says "once per run"; taken literally over every org that is
   288 append-only rows a day per org to record that nothing happened. It still
   writes one on every run where the short circuit did NOT fire, so an org with
   rules has an unbroken per-run record.

**One thing this spec asked for and cycle 2 did not deliver — DELIVERED
2026-08-26, cycle 3.** §5.3 and §9 say the group page "states when membership
was last re-evaluated … a timestamp, never alone". It did not. The sweep's only
durable record was its audit event, and reading it needs `audit.read`, which
the Groups page's `devices.read` gate does not entitle a caller to hold; the
alternative was an org-level `last_evaluated_at`, a column and therefore the
migration cycle 2 did not carry. What the console said instead was the interval
itself, which addressed the same hazard without a number it could not honestly
source — and, as named here, could not distinguish "swept four minutes ago"
from "the worker has been down since Tuesday".

Migration 0081 carries it. `device_group_evaluation` is one row per org, under
the ordinary forced-RLS policy, upserted in the sweep's own transaction:
`last_evaluated_at`, `last_full_pass_at`, the run's four counts, and
`snapshots_read`. `GET /device-groups` returns it as `evaluation` — on the LIST
response rather than a route of its own, because every `device_count` there is
as of that moment and separating the number from the thing that dates it puts
them on two requests either of which can fail alone. **No new permission code**:
the row is org-scoped and discloses strictly less about any device than
`GET /devices` already does, so `devices.read` is the gate, which is the whole
point of not having used the audit log.

Three states on screen, not two. A recent pass renders as a `role="status"`
line naming the age and what the pass did; a pass older than **two** beats
renders as a `role="alert"` — one missed beat is a restart, two consecutive
misses mean the worker is not running — and `evaluation: null` renders as
"never re-evaluated", which is a different sentence from both and must never
appear as "just now". The staleness threshold is a multiple of the response's
own `interval_seconds`, never a literal.

**And the short circuit now writes this row where it still writes no audit
event.** Point 3 above stands for the audit log — 288 append-only rows a day
per org to record that nothing happened — but an org that uses only static
groups is exactly the org whose audit log will never answer "is the worker
alive", so it gets the row.

**And one cost this spec did not quantify — FIXED 2026-08-26, cycle 3, in the
same migration.** The sweep read every active device's latest
`InventoryDocument` every `REEVALUATE_INTERVAL_S`. Matching is cheap —
O(D x G x C) in-process comparisons, well under a second at D=10,000 / G=20 /
C=3 — but the documents are not: a machine's whole collected estate is tens of
kilobytes, so a 10,000-device org moved a few hundred megabytes through this
every five minutes. Batching (`SNAPSHOT_BATCH`) bounds the resident set, not
the transfer.

The fix is the one named here: only devices whose snapshot is newer than the
last sweep, plus every device when a rules document changes. Three rules decide
which pass runs, and the second and third are what keep the first honest:

1. **Incremental** on `watermark_snapshot_id`, an `inventory_snapshots.id` —
   a uuid7, so it orders by creation, and not a timestamp, because
   `taken_at` is the AGENT's clock and one skewed endpoint would move the
   boundary for a whole org.
2. **Full when the rules moved.** `rules_fingerprint` is a sha256 over what
   the evaluator reads — which groups compete, in which precedence order,
   with which documents. A rule decides where a signed job lands, so a rules
   edit must not wait for each device to ingest before it claims anything.
3. **Full when the last full pass is older than `FULL_PASS_INTERVAL_S`** (one
   hour). This is not belt-and-braces; it is the price of the optimisation
   being honest about what it cannot see. "Whose inventory changed" is silent
   about a device whose `status` returns to `active` carrying an old snapshot,
   and about one hand-placed into a static group while a rule elsewhere claims
   it — both of which the full sweep re-homed on its next run. An hour bounds
   that drift instead of leaving it unbounded.

**The boundary lags one run deliberately.** `next_watermark_snapshot_id`
records the newest snapshot id at this run's start and the NEXT run promotes
it. A single-column watermark races with commit lag: a snapshot whose uuid7 was
assigned before the sweep read the maximum but which committed after it would
sit below the boundary forever, and the device would keep a membership its
inventory no longer justifies with nothing on any screen saying so.
Re-evaluating a device twice is free — one already in the group its rule claims
is not moved again — so the cheap fix is the correct one.

**Measured, not asserted.** `ReevaluationResult.snapshots_read` counts the rows
that came off the wire, is recorded on the evaluation row and named in the
audit event, and
`test_device_group_reevaluate.py::test_the_sweep_stops_re_reading_a_fleet_whose_inventory_did_not_move`
seeds a 40-device fleet, measures a full pass's bytes with `pg_column_size`,
and then asserts the steady state reads 0 documents and a single ingest reads
exactly 1. A regression that quietly restores the full read is a number that
stops falling, in a test, rather than a bill somebody notices.

**Deployment.** This document changes no deployed code, so there is no deploy
sequence to run for it — stated rather than omitted, per CLAUDE.md, so a missing
step does not read as a forgotten one. Cycle 1 carries a migration; cycle 2
carries none; each cycle's own commit states its full sequence.

> **CORRECTED 2026-08-26, when cycle 2 was built.** This paragraph used to read
> "cycle 1 carries a migration (`archived_at`, the partial unique index) and
> cycle 2 carries one if OPEN 1 lands on (a) (`precedence`)". That was already
> stale when it was written: §10.1's DECIDED note says `precedence` "lands in
> **cycle 1**, before anything can create a rule-bearing group, or the free
> window closes", and cycle 1's list two paragraphs above names "the
> `precedence` column and the one-line change to `rules.py`'s ordering". It did
> land there, in migration `0072_device_group_lifecycle` together with
> `archived_at` and the partial unique index. **Cycle 2 carries no migration at
> all** — the rules document is an existing jsonb column, the preview writes
> nothing, and the sweep's state is the audit log.
