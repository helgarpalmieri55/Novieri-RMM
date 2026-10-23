"""Reading an org's device groups.

WHAT A DEVICE GROUP IS FOR, because the shape of this query follows from it
rather than from what is easiest to select. `device_groups` is the platform's
targeting and scoping primitive, and four subsystems take a group id:

  * `deploy` -- a deployment or a script run with `target_type="group"`
    resolves to `Device.device_group_id == target_id`
    (`app/modules/deploy/dispatch.py`, `app/modules/deploy/scripts.py`);
  * `patch`, `backup`, `remote` -- each keys a policy on `device_group_id`,
    with NULL meaning "the org default" (`app/modules/*/policy.py`);
  * `health` -- a profile assignment binds to a group and inherits UP
    `parent_group_id` (`app/modules/health/resolution.py`);
  * `inventory` -- a group carrying `rules` claims devices automatically on
    every ingest (`app/modules/inventory/rules.py`).

A fifth, `rbac`, ended with migration 0230: a role grant is scoped to a CLIENT
now (`user_roles.client_id`, MSP clients spec §2.1), so a group no longer gives
or limits permissions and no grant can name one.

Not one of them could be addressed from the console, because no route had ever
returned a group id -- so the only scope expressible was the org default, which
is precisely the row that has no id (audit
`docs/superpowers/audits/2026-08-21-ux-remediation-closeout.md`, rec. 3).

WHY THE MEMBER COUNT IS A SET-BASED SUBQUERY. It is one grouped
`COUNT(devices)` LEFT OUTER JOINed onto `device_groups` once, not a
per-group count in a loop: this is a paged read and a helper per row would put
an N+1 on it, the same reasoning `reporting/definitions/patch_compliance.py`
records for its scan subquery.
"""

import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.i18n import tr
from app.models import (
    BackupPolicy,
    Credential,
    Device,
    DeviceGroup,
    DeviceGroupEvaluation,
    HealthProfileAssignment,
    InventorySnapshot,
    PatchPolicy,
    Policy,
    RemotePolicy,
)
from app.modules.inventory.rules import (
    claim_group,
    matches_document,
    rule_bearing_groups_query,
)
from app.modules.inventory.schema import InventoryDocument

logger = logging.getLogger("its.device_groups")


@dataclass(frozen=True, slots=True)
class DeviceGroupPage:
    """One page of groups, plus the two counts a page cannot carry itself."""

    #: `(group, direct_member_count)`, in `(precedence, name)` order.
    rows: list[tuple[DeviceGroup, int]]
    #: Groups matching BEFORE `limit`/`offset`, so a caller can tell a short
    #: page from the end of the list.
    total: int
    #: Devices in NO group -- the population of the org-default policy.
    ungrouped_device_count: int
    #: The membership sweep's last run, or None before it has ever run for this
    #: org.
    #:
    #: ON THE LIST RESPONSE BECAUSE THE COUNTS ARE ON THE LIST RESPONSE. Every
    #: `device_count` above is as of the last sweep and can be up to
    #: `REEVALUATE_INTERVAL_S` behind; §9 asks for "a timestamp, never alone"
    #: precisely so a stale count is legible rather than read as a wrong one.
    #: Serving it anywhere else would put the number and the thing that dates
    #: it on two different requests, either of which can fail alone.
    evaluation: DeviceGroupEvaluation | None = None


async def list_device_groups(
    session: AsyncSession,
    *,
    org_id: UUID,
    limit: int = 100,
    offset: int = 0,
    include_archived: bool = False,
) -> DeviceGroupPage:
    """This org's device groups in `(precedence, name)` order, each with how
    many devices it directly holds, plus the size of the ungrouped remainder.

    ORDERED BY `(precedence, name)`, AND THAT IS SEMANTIC RATHER THAN TIDY.
    `inventory.rules.evaluate_rules` walks rule-bearing groups in exactly this
    order and the FIRST match claims the device, so this IS the precedence
    order for dynamic membership. A console listing groups any other way would
    answer "which rule wins" differently from the ingest path that actually
    decides it -- which is why this route moved to `(precedence, name)` in the
    same change that gave the evaluator the column (migration 0072).

    It still pages deterministically, because `name` breaks every precedence
    tie and the partial unique index on `(org_id, name) WHERE archived_at IS
    NULL` makes `name` a total order among an org's LIVE groups -- which a
    timestamp is not. `include_archived=True` can therefore, in principle,
    page two archived groups sharing a name; they are ordered by `id` last for
    exactly that case.

    THE COUNT IS DIRECT MEMBERSHIP AND DOES NOT DESCEND `parent_group_id`,
    because that is what a group-targeted job hits: deploy resolves a `"group"`
    target as `Device.device_group_id == target_id`, with no walk over the
    tree. A subtree count would overstate the blast radius of the one action
    the number exists to size. Health assignment DOES inherit down the tree,
    which is why `parent_group_id` is returned as well -- the console can draw
    the hierarchy, and the two semantics stay distinguishable.

    BOTH SIDES CARRY AN EXPLICIT `org_id`, not only the groups. RLS scopes
    both tables already, but `devices.device_group_id` is a bare FK with no
    org-aware constraint (the reason `health/profiles.py::_assert_target_in_org`
    exists), so a neighbour tenant's device row may legally name this org's
    group. Invariant #5 in application code and at the database, not either
    alone.

    ARCHIVED GROUPS ARE EXCLUDED BY DEFAULT (§7.1). That is a change to a
    shipped response's CONTENTS rather than its shape, and it is the behaviour
    the console needs: an archived group is empty and unreferenced by
    construction, so listing it beside live ones would offer a scope that
    reaches nothing. `include_archived=True` brings them back for the screen
    that has to offer an unarchive.
    """
    members = (
        select(
            Device.device_group_id.label("group_id"),
            func.count().label("device_count"),
        )
        .where(Device.org_id == org_id, Device.device_group_id.is_not(None))
        .group_by(Device.device_group_id)
        .subquery()
    )
    query = (
        select(DeviceGroup, func.coalesce(members.c.device_count, 0))
        .join(members, members.c.group_id == DeviceGroup.id, isouter=True)
        .where(DeviceGroup.org_id == org_id)
    )
    if not include_archived:
        query = query.where(DeviceGroup.archived_at.is_(None))

    total = (
        await session.execute(select(func.count()).select_from(query.subquery()))
    ).scalar_one()
    rows = (
        await session.execute(
            query.order_by(
                DeviceGroup.precedence, DeviceGroup.name, DeviceGroup.id
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()
    ungrouped = (
        await session.execute(
            select(func.count())
            .select_from(Device)
            .where(Device.org_id == org_id, Device.device_group_id.is_(None))
        )
    ).scalar_one()

    return DeviceGroupPage(
        rows=[(group, count) for group, count in rows],
        total=total,
        ungrouped_device_count=ungrouped,
        evaluation=await _evaluation_state(session, org_id),
    )


# --------------------------------------------------------------------------- #
# WRITES -- cycle 1 of docs/superpowers/specs/
# 2026-08-22-device-group-management-design.md
#
# WHY EVERY WRITE LIVES HERE AND NOT IN A HANDLER. Each one has to do three
# things together or not at all: change the row, append to the hash-chained
# audit log, and be refused by the same rules whatever route reached it. A
# handler doing that by hand is a handler that will one day lose the append --
# `app/core/errors/triage.py` was written for exactly that reason. Callers hand
# in an `org_session`; the row change and its audit event commit together.
#
# EVERY QUERY BELOW CARRIES AN EXPLICIT `DeviceGroup.org_id == org_id`, and
# `docs/superpowers/audits/2026-08-21-tenancy-test-efficacy.md` §0 is why that
# sentence is here rather than assumed. Everything served through `org_session`
# runs as `app_user` (NOBYPASSRLS), so deleting one of those predicates turns NO
# route test red -- RLS silently covers for it. The tests that hold these are
# driven on the BYPASSRLS admin engine, where the predicate is the only thing
# between a caller and a neighbour's row.
# --------------------------------------------------------------------------- #


class DeviceGroupError(Exception):
    """A refusal with the status the route should answer, and nothing else.

    `blocked_by` is populated only by the archive refusal (§7.2): a 409 that
    says "still in use" without saying by what is a dead end, and this one is
    meant to be a to-do list.
    """

    def __init__(
        self,
        status_code: int,
        detail: str,
        *,
        blocked_by: dict[str, int] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.blocked_by = blocked_by


#: The deepest `parent_group_id` chain a group may sit in.
#:
#: DEFINED HERE AND ASSERTED EQUAL TO `health.resolution.MAX_GROUP_DEPTH`, not
#: imported from it. The kernel must not depend on a module (CLAUDE.md:
#: "modules register into the kernel; they do not fork it"), and two copies of
#: a constant are two chances to disagree -- so `tests/test_device_group_write.py`
#: holds them equal instead. Health's walk TRUNCATES past this bound and drops
#: the device to the org default with a `logger.error`; refusing the reparent
#: here is the fix, and that bound stays as the backstop it is.
MAX_GROUP_DEPTH = 16

#: Devices per `POST /{group_id}/members` request. The bound exists so one
#: audit event's `device_ids` stays a readable record rather than an unbounded
#: blob (§4).
MAX_MEMBERS_PER_REQUEST = 500

_TARGET_TYPE = "device_group"


@dataclass(frozen=True, slots=True)
class MemberResult:
    """One device id's outcome from a bulk membership write.

    Per-id rather than a batch failure, in the shape
    `POST /devices/candidates/promote` already uses: one stale entry in a
    selection must not silently discard the rest, and "already a member" is not
    an error -- two operators looking at the same screen is the normal case.
    """

    device_id: UUID
    moved: bool
    reason: str


async def get_device_group(
    session: AsyncSession, org_id: UUID, group_id: UUID
) -> DeviceGroup:
    """One group of THIS org, archived or not, or a 404.

    404 for both "absent" and "another tenant's", indistinguishably, for the
    reason `app/modules/health/api.py` states: a 403 would confirm the id
    exists somewhere, which is a cross-tenant existence oracle.
    """
    group = (
        await session.execute(
            select(DeviceGroup).where(
                DeviceGroup.id == group_id, DeviceGroup.org_id == org_id
            )
        )
    ).scalar_one_or_none()
    if group is None:
        raise DeviceGroupError(404, tr("no such device group"))
    return group


async def _live_name_is_free(
    session: AsyncSession, org_id: UUID, name: str, *, excluding: UUID | None
) -> bool:
    """Is `name` unused by a LIVE group of this org?

    Checked in application code as well as by the partial unique index, so the
    refusal is a 409 with a sentence in it rather than a 500 out of an
    `IntegrityError` (§5.4). The index is still what makes it true under a
    race; this is what makes it legible.
    """
    query = select(DeviceGroup.id).where(
        DeviceGroup.org_id == org_id,
        DeviceGroup.name == name,
        DeviceGroup.archived_at.is_(None),
    )
    if excluding is not None:
        query = query.where(DeviceGroup.id != excluding)
    return (await session.execute(query.limit(1))).scalar_one_or_none() is None


async def _parent_of(
    session: AsyncSession, org_id: UUID, group_id: UUID
) -> UUID | None:
    return (
        await session.execute(
            select(DeviceGroup.parent_group_id).where(
                DeviceGroup.id == group_id, DeviceGroup.org_id == org_id
            )
        )
    ).scalar_one_or_none()


async def _depth_of(session: AsyncSession, org_id: UUID, group_id: UUID) -> int:
    """How many levels `group_id` sits below the root: a root group is 1.

    The walk is BOUNDED, like health's, because the schema still permits a
    cycle among rows written before this service existed. A cycle here reports
    the bound rather than hanging, and the reparent that would extend it is
    refused on depth.
    """
    depth = 1
    current = await _parent_of(session, org_id, group_id)
    while current is not None and depth <= MAX_GROUP_DEPTH:
        depth += 1
        current = await _parent_of(session, org_id, current)
    return depth


async def _subtree_height(
    session: AsyncSession, org_id: UUID, group_id: UUID
) -> int:
    """Levels BELOW `group_id`: 0 for a leaf.

    Breadth-first and bounded for the same reason `_depth_of` is. This is the
    half a naive depth check misses: moving a shallow subtree under a deep
    parent pushes its LEAVES past the bound even though the group itself is
    fine.
    """
    frontier = [group_id]
    seen = {group_id}
    height = 0
    while frontier and height <= MAX_GROUP_DEPTH:
        children = list(
            (
                await session.execute(
                    select(DeviceGroup.id).where(
                        DeviceGroup.org_id == org_id,
                        DeviceGroup.parent_group_id.in_(frontier),
                    )
                )
            )
            .scalars()
            .all()
        )
        frontier = [child for child in children if child not in seen]
        seen.update(frontier)
        if frontier:
            height += 1
    return height


async def _assert_parent_is_legal(
    session: AsyncSession, org_id: UUID, group_id: UUID, parent_id: UUID
) -> None:
    """Self-parent, cycle, cross-org and depth -- all four at write time.

    `parent_group_id` is a bare self-FK with no constraint of any kind, so the
    schema permits every one of these. `health/resolution.py` SURVIVES a cycle
    (bounded walk, `logger.error`, fall through to the org default) -- but
    surviving it means every device beneath the cycle silently gets the wrong
    profile. Refusing at write time is the fix.
    """
    if parent_id == group_id:
        raise DeviceGroupError(422, tr("a group cannot be its own parent"))

    parent = (
        await session.execute(
            select(DeviceGroup).where(
                DeviceGroup.id == parent_id, DeviceGroup.org_id == org_id
            )
        )
    ).scalar_one_or_none()
    # SAME REFUSAL FOR ABSENT AND FOR ANOTHER TENANT'S. The self-FK is bare,
    # exactly like `devices.device_group_id`, so the schema would happily let
    # this org name a neighbour's group as a parent; application code is what
    # refuses, and it must not distinguish the two cases.
    if parent is None:
        raise DeviceGroupError(422, tr("no such parent group in this organization"))
    if parent.archived_at is not None:
        raise DeviceGroupError(422, tr("the parent group is archived"))

    ancestor = parent.parent_group_id
    walked = 0
    while ancestor is not None and walked <= MAX_GROUP_DEPTH:
        if ancestor == group_id:
            raise DeviceGroupError(
                422,
                tr("that parent is already beneath this group -- the move would make a cycle"),
            )
        ancestor = await _parent_of(session, org_id, ancestor)
        walked += 1

    deepest = (
        await _depth_of(session, org_id, parent_id)
        + 1
        + await _subtree_height(session, org_id, group_id)
    )
    if deepest > MAX_GROUP_DEPTH:
        raise DeviceGroupError(
            422,
            tr(
                "that move would put a group {deepest} levels deep, past the "
                "{max_group_depth}-level bound health profile resolution walks",
                deepest=deepest,
                max_group_depth=MAX_GROUP_DEPTH,
            ),
        )


#: Every live reference the archive refusal counts, as
#: `response key -> (model, column)`.
#:
#: THE LIST IS THE EIGHT FOREIGN KEYS ONTO `device_groups.id`, MINUS THE ROW
#: ITSELF, read out of the schema rather than out of the spec: `devices` and
#: `credentials` and `policies` (migration 0001),
#: `device_groups.parent_group_id` (0001, the self-FK, counted as
#: `child_groups`), `remote_policies` (0010), `patch_policies` (0011),
#: `backup_policies` (0014), `health_profile_assignment` (0042). A ninth,
#: `user_roles.device_group_id`, went with migration 0230: a role grant names a
#: client now, so it can no longer reference a group and cannot block one.
#: Every one is `NO ACTION` -- no migration writes an `ON DELETE` clause
#: anywhere -- which is why a hard `DELETE` has two failure modes and no
#: success mode and this is a soft archive instead.
#:
#: FOUR MORE REFERENCES ARE DELIBERATELY NOT COUNTED, because they are not
#: countable: `deployments.target_id` and `script_runs.target_id`
#: (`deploy/models.py`), `doc_links.target_id` under
#: `target_type = "device_group"`, and `bcp_plan.scope` jsonb entries
#: `{"kind": "group", "id": ...}` all carry a group id in a bare uuid column or
#: inside a document, with no FK at all. They are HISTORY rather than live
#: configuration -- a completed deployment naming this group is the reason the
#: archive is soft, not a reason to refuse it -- and §7.1 turns on the fact
#: that an archived group holds no devices, so a job aimed at one resolves to
#: zero of them.
_BLOCKERS: tuple[tuple[str, Any, Any], ...] = (
    ("devices", Device, Device.device_group_id),
    ("child_groups", DeviceGroup, DeviceGroup.parent_group_id),
    ("policies", Policy, Policy.device_group_id),
    ("patch_policies", PatchPolicy, PatchPolicy.device_group_id),
    ("backup_policies", BackupPolicy, BackupPolicy.device_group_id),
    ("remote_policies", RemotePolicy, RemotePolicy.device_group_id),
    (
        "health_assignments",
        HealthProfileAssignment,
        HealthProfileAssignment.device_group_id,
    ),
    ("credentials", Credential, Credential.device_group_id),
)


async def live_references(
    session: AsyncSession, org_id: UUID, group_id: UUID
) -> dict[str, int]:
    """How many rows of each kind still point at this group.

    Every count carries `org_id` as well as the group id. RLS scopes these
    tables already, but each of these FKs is bare -- there is no org-aware
    constraint on any of them -- so a neighbour tenant's row may legally name
    this org's group, and a count without the predicate would refuse an archive
    on somebody else's data (or, on an admin engine, silently count it).

    `child_groups` counts LIVE children only. An archived child is itself
    empty and unreferenced; `unarchive_device_group` is what stops it coming
    back to life under an archived parent.
    """
    counts: dict[str, int] = {}
    for key, model, column in _BLOCKERS:
        query = (
            select(func.count())
            .select_from(model)
            .where(model.org_id == org_id, column == group_id)
        )
        if model is DeviceGroup:
            query = query.where(DeviceGroup.archived_at.is_(None))
        counts[key] = (await session.execute(query)).scalar_one()
    return counts


async def create_device_group(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    name: str,
    description: str = "",
    parent_group_id: UUID | None = None,
    precedence: int = 0,
    rules: dict[str, Any] | None = None,
) -> DeviceGroup:
    """Create one group. Synchronous and dull, which is the whole point (§5.4).

    A duplicate live name is a 409 with a sentence, not a 500 out of an
    `IntegrityError`.

    `rules` ARRIVES ALREADY VALIDATED, as the `GroupRules` model dumped to a
    dict -- the route's request body is that model, so FastAPI answers the 422
    before this function is reached. Nothing here re-parses it, and nothing
    here interpolates any part of it into a query: `evaluate_rules` selects the
    groups and compares in Python (§5.1).

    A NEW GROUP CANNOT COLLIDE WITH THE STATIC-XOR-DYNAMIC RULE, because it
    holds no devices yet. `update_device_group` is where that refusal lives.
    """
    if not await _live_name_is_free(session, org_id, name, excluding=None):
        raise DeviceGroupError(409, tr("a group of that name already exists"))

    group = DeviceGroup(
        org_id=org_id,
        name=name,
        description=description,
        precedence=precedence,
        rules=rules,
    )
    session.add(group)
    await session.flush()
    if parent_group_id is not None:
        await _assert_parent_is_legal(session, org_id, group.id, parent_group_id)
        group.parent_group_id = parent_group_id
        await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="device_group.created",
        target_type=_TARGET_TYPE,
        target_id=str(group.id),
        metadata={
            "name": group.name,
            "parent_group_id": str(parent_group_id) if parent_group_id else None,
            "precedence": group.precedence,
            "dynamic": rules is not None,
            # THE FULL DOCUMENT, not a flag (§8.2). A rules document decides
            # which machines a signed job reaches; an audit entry recording
            # only that the group "was dynamic" answers nothing an
            # investigator asked.
            "rules": rules,
        },
    )
    await session.flush()
    return group


_EDITABLE = ("name", "description", "parent_group_id", "precedence", "rules")


async def _direct_member_count(
    session: AsyncSession, org_id: UUID, group_id: UUID
) -> int:
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(Device)
                .where(Device.org_id == org_id, Device.device_group_id == group_id)
            )
        ).scalar_one()
    )


async def update_device_group(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    group_id: UUID,
    changes: Mapping[str, Any],
) -> tuple[DeviceGroup, bool]:
    """Apply a partial edit, and say whether health has to be re-pushed.

    Returns `(group, needs_reconcile)`. `needs_reconcile` is true ONLY when
    `parent_group_id` actually moved: health is the one module that PUSHES a
    group binding (`resolve_profile_for_device` walks the parent chain, and
    `sweep.run_policy_reconcile` pushes `health.policy.apply` where a device's
    acknowledged version is behind). Patch, backup and remote all resolve at
    point of use, so a rename or a precedence change needs no push at all --
    and re-pushing on a description edit would be a fleet-wide job storm for a
    typo fix.

    A RENAME IS AN AUDITED RELABELLING AND MOVES NOTHING (§10.1). That is true
    because migration 0072 gave the evaluator `precedence`; before it, the
    alphabet was the precedence list and this route would have needed a
    preview and a confirmation.
    """
    group = await get_device_group(session, org_id, group_id)
    if group.archived_at is not None:
        raise DeviceGroupError(409, tr("this group is archived; unarchive it first"))

    recorded: dict[str, dict[str, Any]] = {}
    reparented = False

    for field in _EDITABLE:
        if field not in changes:
            continue
        new = changes[field]
        old = getattr(group, field)
        if new == old:
            continue
        if field == "name":
            if not await _live_name_is_free(session, org_id, new, excluding=group_id):
                raise DeviceGroupError(409, tr("a group of that name already exists"))
        if field == "parent_group_id":
            if new is not None:
                await _assert_parent_is_legal(session, org_id, group_id, new)
            reparented = True
        if field == "rules" and new is not None:
            # STATIC XOR DYNAMIC, ENFORCED (§5.2, §10.3). `evaluate_rules`
            # OVERWRITES a hand placement whenever a rule matches -- it is a
            # move, not an addition -- so a group that is both would have its
            # operator's edits reverted by a background process with no
            # message. The support ticket says "it forgot my change".
            #
            # THE COUNT IS IN THE MESSAGE because a bare 409 leaves the
            # operator to guess which of their groups is the occupied one, and
            # the remedy is stated because it is not the obvious one: emptying
            # the group is a per-device act by design (§7.2).
            held = await _direct_member_count(session, org_id, group_id)
            if held:
                raise DeviceGroupError(
                    409,
                    (
                        tr("this group already holds {held} hand-placed device", held=held)
                        if held == 1
                        else tr("this group already holds {held} hand-placed devices", held=held)
                    )
                    + tr(
                        " -- membership is either hand-curated or decided by rules, never both, "
                        "because a rule claims a device rather than joining it. Remove them "
                        "first, or put the rules on a new group"
                    ),
                )
        setattr(group, field, new)
        recorded[field] = {
            "from": str(old) if isinstance(old, UUID) else old,
            "to": str(new) if isinstance(new, UUID) else new,
        }

    session.add(group)
    # AUDITED EVEN WHEN `recorded` IS EMPTY. An edit that changed nothing is
    # still an act, and the log records what an operator DID --
    # `app/core/errors/triage.py` states the same reasoning for its idempotent
    # reopen. A trail with holes where the no-ops were is one an investigator
    # cannot reason from.
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="device_group.updated",
        target_type=_TARGET_TYPE,
        target_id=str(group_id),
        metadata={"changed": recorded},
    )
    await session.flush()
    return group, reparented


# --------------------------------------------------------------------------- #
# PREVIEW -- cycle 2 §5.3. The half that makes a rules edit a decision.
# --------------------------------------------------------------------------- #

#: Devices whose inventory documents are held in memory at once.
#:
#: THE BOUND IS ABOUT MEMORY, NOT ABOUT QUERY COST. An `InventoryDocument` is
#: the whole collected estate of one machine -- every installed package, every
#: hardware component -- and is routinely tens of kilobytes. Reading a
#: 10,000-device fleet in one `SELECT` would materialise all of it at once;
#: reading it 500 at a time is 20 round trips and a bounded working set. Both
#: the preview and the re-evaluation sweep walk through here for that reason.
SNAPSHOT_BATCH = 500

#: Devices named in a preview's sample. The COUNTS beside it are exact and
#: unbounded: a screen cannot render four thousand hostnames, and an author
#: must still be told the rule claims four thousand machines.
PREVIEW_SAMPLE = 25


@dataclass(frozen=True, slots=True)
class RuleMatch:
    """One device a candidate rule would claim, named rather than counted.

    `current_group_id` and `current_group_name` are what turn the sample from
    a list into a warning: a device this rule claims is a device it TAKES from
    wherever it is now, and `NULL` is not an absence -- it is the org default
    every policy resolver reads.
    """

    device_id: UUID
    hostname: str | None
    current_group_id: UUID | None
    current_group_name: str | None


@dataclass(frozen=True, slots=True)
class RulePreview:
    """What a candidate rules document reaches, before it reaches it."""

    matched: int
    #: Devices with no `InventorySnapshot`. The evaluator reads DOCUMENTS, so
    #: these can never be claimed by any rule -- and an operator who is not
    #: told concludes their rule is wrong (§5.3).
    devices_without_inventory: int
    sample: list[RuleMatch]
    sample_truncated: bool


async def _latest_documents(
    session: AsyncSession, org_id: UUID, device_ids: Sequence[UUID]
) -> tuple[dict[UUID, InventoryDocument], int]:
    """The newest snapshot per device, rebuilt into the ingest's own document,
    AND THE NUMBER OF ROWS THAT CAME OFF THE WIRE TO BUILD IT.

    THE SECOND HALF IS NOT BOOKKEEPING. `InventoryDocument` is a machine's
    whole collected estate and this is the platform's largest recurring read;
    the count is what the incremental sweep records as `snapshots_read`, so a
    regression that quietly restores the full read is a number that stops
    falling rather than a bill somebody notices. It counts ROWS, not parsed
    documents: an unparseable snapshot cost its bytes whether or not this
    control plane could read it.

    `DISTINCT ON (device_id) ... ORDER BY device_id, id DESC` is one query for
    the whole batch rather than one per device, and `id` is a uuid7 so it
    orders by creation -- the same ordering `inventory/ingest.py` uses to prune
    to `SNAPSHOT_KEEP`.

    THE ROUND TRIP IS EXACT: the ingest stores `doc.model_dump(mode="json")`,
    so `InventoryDocument(**raw)` gives back the document the evaluator saw.
    A snapshot this control plane can no longer parse -- an older schema, a
    hand-edited row -- is SKIPPED rather than raised on, and the device then
    counts as having no inventory, which is the true statement about it.
    """
    if not device_ids:
        return {}, 0
    rows = (
        await session.execute(
            select(InventorySnapshot.device_id, InventorySnapshot.raw)
            .where(
                InventorySnapshot.org_id == org_id,
                InventorySnapshot.device_id.in_(device_ids),
            )
            .distinct(InventorySnapshot.device_id)
            .order_by(InventorySnapshot.device_id, InventorySnapshot.id.desc())
        )
    ).all()
    documents: dict[UUID, InventoryDocument] = {}
    for device_id, raw in rows:
        try:
            documents[device_id] = InventoryDocument(**raw)
        except ValidationError:
            logger.warning(
                "unparseable inventory snapshot; the device counts as "
                "un-collected for rule evaluation",
                # RAW UUIDs, NOT `str(...)`. `test_logging_discipline.py`
                # forbids `str()`/`repr()` in an `extra=` value outright: it is
                # a syntactic rule, because the shape it exists to stop --
                # `str(exc)` smuggling an exception's message into a field --
                # is indistinguishable from this one at the AST. Nothing is
                # lost by obeying it: `_COERCIBLE` in `core/logging.py` already
                # holds UUID, so the formatter renders these itself, the way
                # `modules/health/actions.py` passes its ids.
                extra={"device_id": device_id, "org_id": org_id},
            )
    return documents, len(rows)


async def _active_device_batch(
    session: AsyncSession,
    org_id: UUID,
    *,
    after: UUID | None,
    changed_since: UUID | None = None,
) -> list[Device]:
    """One batch of this org's active devices, for a keyset walk.

    `changed_since`, WHEN GIVEN, NARROWS THIS TO DEVICES WHOSE INVENTORY MOVED.
    It is an `inventory_snapshots.id` -- a uuid7, so it orders by creation --
    and the `EXISTS` below is the whole of incremental re-evaluation: a device
    whose newest snapshot predates the boundary cannot match a rule differently
    from the way it matched last time, so reading its document again is pure
    transfer. The predicate rides `ix_inventory_snapshots_device
    (device_id, id)`. `None` means the full pass, which is what the preview
    always wants and what the sweep falls back to.

    ITS OWN `org_id` PREDICATE, on the subquery as well as the outer select.
    RLS scopes both tables already; this is the belt, and
    `test_device_group_reevaluate.py` runs it on the BYPASSRLS role where it is
    the only thing scoping anything.

    ACTIVE ONLY, matching `health/sweep.py`'s reconcile, because a
    decommissioned device is not a machine any job should reach. Keyset on `id`
    rather than `OFFSET`, so a fleet being enrolled underneath the walk cannot
    make it skip a page.

    THE `org_id` PREDICATE APPEARS EXACTLY ONCE IN THIS FILE'S DEVICE WALK, and
    that is the reason both the preview and the sweep come through here rather
    than each writing their own. Each of them started as TWO queries -- ids,
    then rows -- with the predicate on both, so deleting either left the
    tenancy test green and the invariant held by nothing in particular. A
    predicate that has a spare is a predicate no test can falsify, and
    `docs/superpowers/audits/2026-08-21-tenancy-test-efficacy.md` is about
    exactly that class of comfort. One query, one predicate, one falsifier.

    IT RETURNS ORM ENTITIES rather than columns because the sweep MUTATES
    `device_group_id` on them; the preview reads the same three attributes off
    the same objects and writes nothing.
    """
    query = select(Device).where(Device.org_id == org_id, Device.status == "active")
    if after is not None:
        query = query.where(Device.id > after)
    if changed_since is not None:
        query = query.where(
            select(InventorySnapshot.id)
            .where(
                InventorySnapshot.device_id == Device.id,
                InventorySnapshot.org_id == org_id,
                InventorySnapshot.id > changed_since,
            )
            .exists()
        )
    return list(
        (await session.execute(query.order_by(Device.id).limit(SNAPSHOT_BATCH)))
        .scalars()
        .all()
    )


async def preview_rules(
    session: AsyncSession,
    *,
    org_id: UUID,
    rules: dict[str, Any],
    group_id: UUID | None = None,
) -> RulePreview:
    """What this candidate rules document would claim, right now. Writes
    nothing.

    WHY THIS ROUTE EXISTS AT ALL, and it is not convenience. A device group is
    an authorization surface: `deploy/dispatch.py` resolves a
    `target_type="group"` job to `Device.device_group_id == target_id`, so a
    rule that matches more machines than its author intended is a way to
    dispatch a signed job to machines nobody chose. §5.3 chose a BACKGROUND
    re-evaluation over an immediate one precisely so that a form submit is
    never a fleet-wide policy change -- and a background write with no preview
    would be the same gamble taken five minutes later. The preview is the other
    half of that decision, not a nicety attached to it.

    IT RUNS THE SWEEP'S MATCHER, NOT A SECOND ONE. `matches_document` is the
    function `evaluate_rules` calls, over the same stored snapshots the sweep
    replays. An author shown one number and given another has been shown a
    guess.

    WHAT IT DELIBERATELY DOES NOT MODEL: precedence. This answers "which
    devices does this document match", not "which devices would end up in this
    group" -- a device matched by a lower-precedence group's rule is claimed by
    that one instead. Modelling the whole evaluator would mean re-running every
    rule in the org on every keystroke, and the honest answer for an author is
    the one that does not silently shrink: the set this rule reaches is the set
    they are responsible for.

    `group_id`, when given, must resolve in this org -- same refusal for absent
    and for a neighbour's, indistinguishably, because a 403 would confirm the
    id exists somewhere.
    """
    if group_id is not None:
        await get_device_group(session, org_id, group_id)

    names = {
        row_id: name
        for row_id, name in (
            await session.execute(
                select(DeviceGroup.id, DeviceGroup.name).where(
                    DeviceGroup.org_id == org_id
                )
            )
        ).all()
    }

    matched = 0
    without_inventory = 0
    sample: list[RuleMatch] = []
    after: UUID | None = None
    while True:
        batch = await _active_device_batch(session, org_id, after=after)
        if not batch:
            break
        after = batch[-1].id
        documents, _rows_read = await _latest_documents(
            session, org_id, [device.id for device in batch]
        )
        for device in batch:
            doc = documents.get(device.id)
            if doc is None:
                without_inventory += 1
                continue
            if not matches_document(rules, doc):
                continue
            matched += 1
            if len(sample) < PREVIEW_SAMPLE:
                current = device.device_group_id
                sample.append(
                    RuleMatch(
                        device_id=device.id,
                        hostname=device.hostname,
                        current_group_id=current,
                        current_group_name=(
                            names.get(current) if current is not None else None
                        ),
                    )
                )

    return RulePreview(
        matched=matched,
        devices_without_inventory=without_inventory,
        sample=sample,
        sample_truncated=matched > len(sample),
    )


# --------------------------------------------------------------------------- #
# RE-EVALUATION -- cycle 2 §5.3. The half that makes a rule take effect at all.
# --------------------------------------------------------------------------- #

#: The beat cadence, and therefore the LAG a membership change costs.
#:
#: THE SPEC FIXES IT AT 300s AND THE CONSOLE SAYS SO. The write route does not
#: enqueue this sweep, and that is deliberate rather than an omission: no route
#: in this codebase enqueues a Celery task -- `grep` for `.delay(` and
#: `apply_async` across `app/` returns nothing, everything is beat-driven --
#: and introducing request-to-broker enqueueing to shave a bounded five minutes
#: off a membership update is a larger change, with its own failure modes, than
#: the feature it would serve.
#:
#: THE WINDOW IS A DISPLAY LAG, NOT A WRONG ACTION. `patch/policy.py`,
#: `backup/policy.py`, `remote/policy.py` and `deploy/dispatch.py` all resolve
#: at POINT OF USE by reading `Device.device_group_id` when the job is built,
#: so a device that has not yet been re-homed simply gets the policy it is on.
#: Health is the exception -- it PUSHES -- and this sweep reconciles it.
REEVALUATE_INTERVAL_S = 300.0

#: Per-device moves named in one sweep's audit event. The COUNT beside the list
#: is exact and unbounded.
#:
#: ONE EVENT PER RUN, NOT ONE PER DEVICE, and the reason is mechanical:
#: `audit_service.record` takes a per-org `pg_advisory_xact_lock` and reads the
#: chain head on every append, so 5,000 devices would be 5,000 serialized head
#: reads inside a single transaction. The truncation is STATED in the event
#: (`moves_truncated`) rather than hidden, because an audit entry that implies
#: a completeness it does not have is worse than one that admits its bound.
MOVES_RECORDED = 100

#: How stale the last FULL pass may get before the next run is forced to be one.
#:
#: THE WATERMARK CANNOT SEE EVERYTHING, AND THIS IS WHERE THAT IS PAID FOR. An
#: incremental pass asks "whose inventory changed", which is the right question
#: for the case that motivated it and is silent about three others:
#:
#: * a device whose `status` returns to `active` carrying an old snapshot -- it
#:   was invisible to the walk while decommissioned and its document did not
#:   move when it came back;
#: * a device hand-placed into a STATIC group while a rule elsewhere claims it
#:   (§5.2 forbids hand-placement INTO a rule-bearing group, not out of the
#:   reach of a rule) -- the full sweep re-homed it on the next run and the
#:   incremental one never looks at it again;
#: * anything a future change adds to the inputs of `claim_group` that is not
#:   the snapshot and not the rules document.
#:
#: A watermark that quietly stopped answering those would be the worst kind of
#: optimisation: correct on the case it was written for and silently wrong
#: elsewhere, with a membership on screen that no screen says is stale. One
#: hour bounds all three -- eleven cheap runs and one full one per hour at a
#: 300s beat, so the transfer this exists to cut still falls by an order of
#: magnitude, and the drift it cannot see is bounded by an hour rather than by
#: nothing.
FULL_PASS_INTERVAL_S = 3600.0


@dataclass(frozen=True, slots=True)
class MembershipMove:
    """One device the sweep re-homed, and where from.

    `from_group_id` is `None` when the device was on the ORG DEFAULT -- which
    is a policy set, not an absence, so "moved from nothing" would be the wrong
    reading of the audit entry.
    """

    device_id: UUID
    from_group_id: UUID | None
    to_group_id: UUID


@dataclass(frozen=True, slots=True)
class ReevaluationResult:
    """What one org's sweep did, in the shape §8.2's audit event records."""

    groups_evaluated: int
    devices_evaluated: int
    devices_moved: int
    devices_without_inventory: int
    moves: list[MembershipMove]
    moves_truncated: bool
    #: Inventory documents read off the wire. THE NUMBER THE WATERMARK EXISTS
    #: TO MOVE, reported rather than inferred so a test can measure the
    #: difference instead of asserting it.
    snapshots_read: int = 0
    #: Whether this run read every active device. False is the steady state
    #: once a watermark exists and the rules have not moved.
    full_pass: bool = True


def rules_fingerprint(groups: Sequence[DeviceGroup]) -> str:
    """A sha256 over what the evaluator actually reads, in the order it reads it.

    WHY A FINGERPRINT AND NOT `max(updated_at)`. `updated_at` moves on a
    description edit and does not move when a group is ARCHIVED out of the
    rule-bearing set by another path, so it both over- and under-reports the
    thing that matters. This hashes the inputs themselves: which groups
    compete, in which precedence order, with which documents. Change any of
    them and the next pass is full; change a name or a description and it is
    not.

    `sort_keys=True` because a rules document is JSONB and Postgres does not
    promise key order back, so an unsorted dump would fingerprint the same
    document differently on different reads and force a full pass forever.
    """
    payload = [
        {
            "id": str(group.id),
            "precedence": group.precedence,
            "rules": group.rules,
        }
        for group in groups
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()


async def _evaluation_state(
    session: AsyncSession, org_id: UUID
) -> DeviceGroupEvaluation | None:
    """This org's last-run row, or None before its first sweep.

    THE `org_id` PREDICATE IS THE WHOLE QUERY and is still not redundant: RLS
    scopes the table, and on the admin engine a missing predicate would read a
    NEIGHBOUR's watermark -- which is worse than reading nothing, because a
    watermark from a busier tenant skips this tenant's devices silently.
    """
    return (
        await session.execute(
            select(DeviceGroupEvaluation).where(DeviceGroupEvaluation.org_id == org_id)
        )
    ).scalar_one_or_none()


async def _max_snapshot_id(session: AsyncSession, org_id: UUID) -> UUID | None:
    """The newest `inventory_snapshots.id` this org holds, or None.

    `ORDER BY id DESC LIMIT 1`, NOT `max(id)`. PostgreSQL 16 has no `max(uuid)`
    aggregate at all -- `uuid` gained one only in 18 -- so the obvious spelling
    is an `UndefinedFunctionError` at runtime rather than a slower plan. This
    one is a single backward seek on `ix_inventory_snapshots_org_id`
    (`(org_id, id)`, migration 0081), which that index exists for: without it
    the planner walks the primary key backwards filtering on `org_id`, and for
    a tenant that has ingested nothing while its neighbours have, that walk is
    the whole table.

    A UUID7 AND NOT A TIMESTAMP, DELIBERATELY. `inventory_snapshots.taken_at`
    is the AGENT's clock, so one skewed endpoint would move the boundary for
    the entire organisation; `id` is assigned server-side and orders by
    creation, which is the property `_latest_documents` already relies on.
    """
    return (
        await session.execute(
            select(InventorySnapshot.id)
            .where(InventorySnapshot.org_id == org_id)
            .order_by(InventorySnapshot.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _record_evaluation(
    session: AsyncSession,
    *,
    org_id: UUID,
    now: datetime,
    result: ReevaluationResult,
    watermark: UUID | None,
    next_watermark: UUID | None,
    fingerprint: str,
    last_full_pass_at: datetime | None,
) -> None:
    """Upsert this org's one last-run row.

    `ON CONFLICT (org_id) DO UPDATE` against the PRIMARY KEY, so a first sweep
    and a thousandth are the same statement and neither has a read-then-write
    race in the middle.

    IT WRITES IN THE CALLER'S TRANSACTION, like everything else here. The row
    change, the audit append and this land together or not at all -- a
    `last_evaluated_at` that survived a rolled-back sweep would be the console
    reporting a pass that did not happen, which is the exact failure the field
    exists to make visible.
    """
    values = {
        "org_id": org_id,
        "last_evaluated_at": now,
        "last_full_pass_at": last_full_pass_at,
        "watermark_snapshot_id": watermark,
        "next_watermark_snapshot_id": next_watermark,
        "rules_fingerprint": fingerprint,
        "groups_evaluated": result.groups_evaluated,
        "devices_evaluated": result.devices_evaluated,
        "devices_moved": result.devices_moved,
        "devices_without_inventory": result.devices_without_inventory,
        "snapshots_read": result.snapshots_read,
        "full_pass": result.full_pass,
    }
    await session.execute(
        pg_insert(DeviceGroupEvaluation)
        .values(**values)
        .on_conflict_do_update(
            index_elements=["org_id"],
            set_={key: value for key, value in values.items() if key != "org_id"},
        )
    )


async def reevaluate_org_membership(
    session: AsyncSession, *, org_id: UUID
) -> ReevaluationResult:
    """Re-home this org's active devices according to its live rule groups.

    WHY THIS EXISTS RATHER THAN "IT TAKES EFFECT ON THE NEXT INGEST". Nothing
    schedules an ingest. `inventory.collect` is registered, directly
    dispatchable and gated, and no beat dispatches it; the agent's scheduler
    wheel registers no inventory task; `DEFAULT_SWEEP_POLICY` has been seeded
    into `policies` since Phase 0 and is read by nothing. For an unattended
    fleet "the next ingest" is never, and a rules editor whose group sits at
    zero devices with no explanation is worse than no rules editor.

    THE SHORT CIRCUIT IS THE FIRST THING IT DOES, and it is one `COUNT`. On a
    deployment that uses only static groups -- which is every deployment until
    somebody writes a rule -- this function reads one number and returns. It
    also keeps the audit log from accruing an event per org per run saying
    nothing happened.

    COST, STATED RATHER THAN DISCOVERED. Per org with at least one rule group:
    one `COUNT`, one group `SELECT`, then the device walk in batches of
    `SNAPSHOT_BATCH` -- two queries per batch, so `2 * ceil(D / 500) + 2`
    round trips for D active devices. Matching itself is O(D x G x C) pure
    Python comparisons for G rule groups of C conditions, which for
    D=10,000, G=20, C=3 is 600,000 dict lookups and string compares: well
    under a second. THE REAL COST IS THE I/O -- every active device's latest
    `InventoryDocument` is read, and that document is a machine's whole
    collected estate (every installed package, every hardware component), so a
    10,000-device org moves on the order of a few hundred megabytes through
    this function every `REEVALUATE_INTERVAL_S`. Batching bounds the RESIDENT
    set, not the transfer.

    THE TRANSFER IS NOW INCREMENTAL, AND THAT PARAGRAPH USED TO SAY IT WAS NOT.
    It read "what would fix the transfer and is not built here: re-evaluating
    only devices whose snapshot is newer than the last sweep, plus every device
    when a rules document changes. That needs a watermark this cycle has
    nowhere to store." `device_group_evaluation` (migration 0081) is that
    store, and this is the rule:

    * **FULL PASS** when the org has no evaluation row yet, or no watermark, or
      its `rules_fingerprint` differs from the live rule-bearing groups' -- a
      rule decides where a signed job lands, so a rules change must not wait
      for an ingest -- or the last full pass is older than
      `FULL_PASS_INTERVAL_S`, which is what bounds the three drifts a watermark
      cannot see (that constant's note lists them).
    * **INCREMENTAL** otherwise: only devices holding a snapshot newer than
      `watermark_snapshot_id`. A device whose newest document predates the
      boundary cannot match a rule differently from the way it matched last
      time, so reading it again is pure transfer.

    THE BOUNDARY LAGS ONE RUN ON PURPOSE. `next_watermark_snapshot_id` records
    the maximum snapshot id seen at THIS run's start, and the next run promotes
    it. A single-column watermark would race with commit lag: a snapshot whose
    uuid7 was assigned before this read `max(id)` but which committed after it
    would sit below the boundary forever, and the device would keep a
    membership its inventory no longer justifies with nothing on screen saying
    so. Evaluating a device twice is free -- one already in the group its rule
    claims is not moved again -- so the cheap fix is the correct one.

    EVERY RUN WRITES `last_evaluated_at`, INCLUDING THE SHORT-CIRCUIT RUN. The
    short circuit still writes no AUDIT event (§8.2 taken literally over every
    org is 288 rows a day saying nothing happened), but it does write this row,
    because "the worker is alive" is exactly what an org with no rules still
    needs the Groups page to be able to say -- and `devices.read` entitles a
    caller to this row where it does not entitle them to the audit log.

    IT DOES NOT COMMIT. The caller owns the transaction, so the row changes,
    the audit event and the evaluation row land together or not at all -- and
    the caller is what publishes the health reconcile's signed jobs, AFTER the
    commit.
    """
    now = datetime.now(UTC)
    state = await _evaluation_state(session, org_id)
    # TAKEN BEFORE THE WALK, so a snapshot written while this runs is above the
    # boundary rather than below it and is picked up next time.
    next_watermark = await _max_snapshot_id(session, org_id)

    groups = list(
        (await session.execute(rule_bearing_groups_query(org_id))).scalars().all()
    )
    fingerprint = rules_fingerprint(groups)

    if not groups:
        # NOTHING TO EVALUATE, BUT SOMETHING TO RECORD. The watermark still
        # advances: an org that adds its first rule tomorrow should not then
        # replay every snapshot ever written, and the full pass its empty
        # fingerprint forces is the one that gets it right.
        empty = ReevaluationResult(0, 0, 0, 0, [], False, snapshots_read=0, full_pass=True)
        await _record_evaluation(
            session,
            org_id=org_id,
            now=now,
            result=empty,
            watermark=state.next_watermark_snapshot_id if state else None,
            next_watermark=next_watermark,
            fingerprint=fingerprint,
            last_full_pass_at=now,
        )
        return empty

    stale_full_pass = (
        state is None
        or state.last_full_pass_at is None
        or (now - state.last_full_pass_at) >= timedelta(seconds=FULL_PASS_INTERVAL_S)
    )
    full_pass = (
        state is None
        or state.watermark_snapshot_id is None
        or state.rules_fingerprint != fingerprint
        or stale_full_pass
    )
    changed_since = None if full_pass else state.watermark_snapshot_id  # type: ignore[union-attr]

    evaluated = 0
    without_inventory = 0
    moved = 0
    snapshots_read = 0
    moves: list[MembershipMove] = []
    after: UUID | None = None
    while True:
        batch = await _active_device_batch(
            session, org_id, after=after, changed_since=changed_since
        )
        if not batch:
            break
        after = batch[-1].id
        documents, rows_read = await _latest_documents(
            session, org_id, [device.id for device in batch]
        )
        snapshots_read += rows_read
        for device in batch:
            evaluated += 1
            doc = documents.get(device.id)
            if doc is None:
                without_inventory += 1
                continue
            claimed = claim_group(groups, doc)
            # NO MATCH PRESERVES THE PLACEMENT. `evaluate_rules` only ever
            # ASSIGNS -- it never unassigns -- and §7.2 depends on exactly
            # that: clearing a group's rules has to leave its members where
            # they are, or a rule-bearing group can never be emptied and
            # therefore never archived.
            if claimed is None or device.device_group_id == claimed.id:
                continue
            if len(moves) < MOVES_RECORDED:
                moves.append(
                    MembershipMove(
                        device_id=device.id,
                        from_group_id=device.device_group_id,
                        to_group_id=claimed.id,
                    )
                )
            device.device_group_id = claimed.id
            session.add(device)
            moved += 1

    await session.flush()
    await audit_service.record(
        session,
        org_id=org_id,
        # NO USER PRESSED ANYTHING AT THE MOMENT THIS RAN (§8.2). The write
        # event carries the count the operator CONFIRMED; this one carries what
        # actually moved, and the two can legitimately differ because the fleet
        # changes in between.
        actor_type="system",
        actor_id=None,
        action="device_group.membership_reevaluated",
        # The subject is the org's whole membership, not any one group.
        target_type="organization",
        target_id=str(org_id),
        metadata={
            "groups_evaluated": len(groups),
            "devices_evaluated": evaluated,
            "devices_moved": moved,
            "devices_without_inventory": without_inventory,
            # WHICH DEVICES THIS RUN WAS EVEN ALLOWED TO MOVE. An investigator
            # reading "devices_evaluated: 3" on a 4,000-device fleet must be
            # able to tell an incremental pass from a broken one, and
            # `snapshots_read` is what makes the transfer claim auditable
            # rather than asserted.
            "full_pass": full_pass,
            "snapshots_read": snapshots_read,
            "moves": [
                {
                    "device_id": str(move.device_id),
                    "from_group_id": (
                        str(move.from_group_id) if move.from_group_id else None
                    ),
                    "to_group_id": str(move.to_group_id),
                }
                for move in moves
            ],
            "moves_truncated": moved > len(moves),
        },
    )
    result = ReevaluationResult(
        groups_evaluated=len(groups),
        devices_evaluated=evaluated,
        devices_moved=moved,
        devices_without_inventory=without_inventory,
        moves=moves,
        moves_truncated=moved > len(moves),
        snapshots_read=snapshots_read,
        full_pass=full_pass,
    )
    await _record_evaluation(
        session,
        org_id=org_id,
        now=now,
        result=result,
        # THE PROMOTION. The boundary the NEXT run selects on is the maximum
        # this run started from -- one interval behind the one it just
        # recorded -- which is the commit-lag overlap the class docstring on
        # `DeviceGroupEvaluation` explains.
        watermark=(state.next_watermark_snapshot_id if state else None),
        next_watermark=next_watermark,
        fingerprint=fingerprint,
        last_full_pass_at=(
            now if full_pass else (state.last_full_pass_at if state else None)
        ),
    )
    await session.flush()

    return result


async def archive_device_group(
    session: AsyncSession, *, org_id: UUID, actor_id: UUID, group_id: UUID
) -> DeviceGroup:
    """Soft-delete a group, or refuse and enumerate what is holding it (§7).

    WHY THE REFUSAL RATHER THAN RE-HOMING THE MEMBERS TO `NULL`. `NULL` is not
    an absence: `patch/policy.py`, `backup/policy.py`, `remote/policy.py` and
    `health/resolution.py::_org_default_assignment` all read
    `device_group_id IS NULL` as the ORG DEFAULT. So a one-click archive of a
    300-device group would be a 300-device patch-policy change, a
    backup-policy change and a remote-policy change, delivered by a button
    labelled Delete. The operator can still have exactly that -- by emptying
    the group first, in an act that says what it is and produces its own audit
    event and its own count on screen.

    NO `force` PARAMETER. A flag that turns a refusal into the thing the
    refusal exists to prevent is the refusal not existing.
    """
    group = await get_device_group(session, org_id, group_id)
    if group.archived_at is not None:
        raise DeviceGroupError(409, tr("this group is already archived"))

    blocked_by = await live_references(session, org_id, group_id)
    if any(blocked_by.values()):
        raise DeviceGroupError(
            409,
            tr(
                "this group is still in use -- every reference below has to be moved or "
                "removed first, because a device leaving a group does not lose policy, it "
                "falls back to the org default",
            ),
            blocked_by=blocked_by,
        )

    group.archived_at = datetime.now(UTC)
    session.add(group)
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="device_group.archived",
        target_type=_TARGET_TYPE,
        target_id=str(group_id),
        # Zero by construction -- the refusal above is what makes it so. It is
        # written down anyway, because an audit entry asserting the group was
        # empty is worth more than one that leaves it to be inferred.
        metadata={"name": group.name, "member_count": blocked_by["devices"]},
    )
    await session.flush()
    return group


async def unarchive_device_group(
    session: AsyncSession, *, org_id: UUID, actor_id: UUID, group_id: UUID
) -> DeviceGroup:
    """Reverse an archive (§7.3).

    `bd04957`'s reason, quoted in the UX closeout for exactly this shape: a
    one-way state change with no way back is the defect (B76), and the undo is
    cheaper to build now than to retrofit.

    Two refusals, and both come from the partial unique index that makes the
    archive safe in the first place. A live group may have taken this one's
    name while it was gone; and a group whose parent was archived after it
    cannot come back to life beneath an archived parent, because that is the
    orphan §6.3 says cannot occur.
    """
    group = await get_device_group(session, org_id, group_id)
    if group.archived_at is None:
        raise DeviceGroupError(409, tr("this group is not archived"))
    if not await _live_name_is_free(session, org_id, group.name, excluding=group_id):
        raise DeviceGroupError(
            409,
            tr(
                "a live group already holds that name -- rename that one, or this group cannot "
                "come back under its own name",
            ),
        )
    if group.parent_group_id is not None:
        parent = (
            await session.execute(
                select(DeviceGroup).where(
                    DeviceGroup.id == group.parent_group_id,
                    DeviceGroup.org_id == org_id,
                )
            )
        ).scalar_one_or_none()
        if parent is None or parent.archived_at is not None:
            raise DeviceGroupError(
                409,
                tr(
                    "its parent group is archived -- unarchive the parent first, or this group "
                    "comes back with nothing above it",
                ),
            )

    group.archived_at = None
    session.add(group)
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="device_group.unarchived",
        target_type=_TARGET_TYPE,
        target_id=str(group_id),
        metadata={"name": group.name, "member_count": 0},
    )
    await session.flush()
    return group


async def _writable_static_group(
    session: AsyncSession, org_id: UUID, group_id: UUID
) -> DeviceGroup:
    """The group a membership write may touch, or the refusal saying why not.

    A RULE-BEARING GROUP REFUSES HAND PLACEMENT, and that is `787461f`'s point
    turned into an enforcement: `evaluate_rules` OVERWRITES a hand placement on
    the next ingest, so offering the edit would be offering an edit the
    platform reverts with no message. No route in cycle 1 can create such a
    group -- `rules` is rejected on create and update, and the editor is cycle
    2 -- so this can only fire on a row inserted by hand with SQL. It is here
    because the rule is about the evaluator, not about which cycle wrote the
    row.
    """
    group = await get_device_group(session, org_id, group_id)
    if group.archived_at is not None:
        raise DeviceGroupError(409, tr("this group is archived; unarchive it first"))
    if group.rules is not None:
        raise DeviceGroupError(
            409,
            tr(
                "this group's membership is decided by rules -- a hand placement here is "
                "reverted on the next inventory ingest",
            ),
        )
    return group


async def add_group_members(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    group_id: UUID,
    device_ids: Sequence[UUID],
) -> list[MemberResult]:
    """Put devices into this group, answering per id (§4, §5.2).

    MEMBERSHIP IS WRITTEN ON THE GROUP, NOT ON THE DEVICE, and that is a
    choice. `PATCH /devices/{id}` gaining a `device_group_id` was the other
    option: it is one device per request on a screen whose whole job is bulk,
    and it is gated `assets.manage` -- a code `technician` holds -- so the
    device-side field would hand fleet re-segmentation to a role §8.1
    deliberately keeps out of it.
    """
    if len(device_ids) > MAX_MEMBERS_PER_REQUEST:
        raise DeviceGroupError(
            422, tr(
                "at most {max_members_per_request} devices per request",
                max_members_per_request=MAX_MEMBERS_PER_REQUEST,
            )
        )
    group = await _writable_static_group(session, org_id, group_id)

    results: list[MemberResult] = []
    moved: list[str] = []
    for device_id in device_ids:
        device = (
            await session.execute(
                select(Device).where(Device.id == device_id, Device.org_id == org_id)
            )
        ).scalar_one_or_none()
        if device is None:
            results.append(MemberResult(device_id, False, "no such device"))
            continue
        if device.device_group_id == group.id:
            results.append(MemberResult(device_id, False, "already a member"))
            continue
        device.device_group_id = group.id
        session.add(device)
        results.append(MemberResult(device_id, True, "added"))
        moved.append(str(device_id))

    await session.flush()
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="device_group.members_added",
        target_type=_TARGET_TYPE,
        target_id=str(group_id),
        metadata={"device_ids": moved},
    )
    await session.flush()
    return results


async def remove_group_member(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    group_id: UUID,
    device_id: UUID,
) -> None:
    """Take one device out of this group.

    IT DOES NOT LOSE POLICY, IT CHANGES POLICY. `device_group_id IS NULL` is
    the org default to every policy resolver in the platform, so this is a
    policy change for that machine and the console says so before it happens.
    """
    group = await _writable_static_group(session, org_id, group_id)
    device = (
        await session.execute(
            select(Device).where(Device.id == device_id, Device.org_id == org_id)
        )
    ).scalar_one_or_none()
    if device is None or device.device_group_id != group.id:
        raise DeviceGroupError(404, tr("that device is not a member of this group"))

    device.device_group_id = None
    session.add(device)
    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="device_group.members_removed",
        target_type=_TARGET_TYPE,
        target_id=str(group_id),
        metadata={"device_ids": [str(device_id)]},
    )
    await session.flush()
