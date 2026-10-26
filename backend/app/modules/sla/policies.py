"""Policy CRUD and (queue, priority) resolution (docs/modules/sla/spec.md
§2.4, §3; Task 4).

Mirrors `app.modules.sla.calendars`'s shape: an explicit `org_id` predicate on
every query (invariant #5), audit-then-flush on every mutation (invariant #4),
and `ON CONFLICT DO NOTHING` + `RETURNING id` on create rather than
check-then-insert.

FOUR PARTIAL UNIQUE INDEXES, NOT ONE CONSTRAINT (spec §2.4, migrations 0033
and 0036). A NULL never collides with another NULL in a plain unique
constraint, so `sla_policy` is protected by four separate partial unique
INDEXES, one per §3 resolution level (`app/modules/sla/models.py`'s
`SlaPolicy.__table_args__`).
Critically, these are plain `CREATE UNIQUE INDEX ... WHERE ...` statements,
NOT named table constraints -- confirmed directly against a live Postgres
instance: `INSERT ... ON CONFLICT ON CONSTRAINT uq_sla_policy_...` raises
`constraint "uq_sla_policy_..." does not exist`, because a partial unique
index with no backing `ADD CONSTRAINT` has no `pg_constraint` row for
`ON CONSTRAINT` to name. `create_policy` therefore targets each level with
`index_elements=`/`index_where=` (Postgres infers the matching partial index
from the column list and predicate), never `constraint=` -- `calendars.py`'s
`create_calendar`/`add_holiday` can use `constraint=` because
`uq_sla_calendar_org_name`/`uq_sla_calendar_holiday_cal_date` are real
`UniqueConstraint`s, not partial indexes; that is not this shape.

Confirmed separately (same live-instance check): asyncpg's
`UniqueViolationError.constraint_name` for a violation of a plain unique
INDEX (not a table constraint) is still the INDEX's own name -- Postgres
names the violated index either way. `update_policy`'s fallback narrowing
(mirroring `calendars.py`'s Task 3 review F5 discipline) matches against the
four index names in `_POLICY_UNIQUE_INDEX_NAMES` for exactly this reason.

UNIQUENESS IS SCOPED TO ACTIVE POLICIES (migration 0036, Task 4 review F2).
Every one of the four index predicates ends `AND active`. §3 only ever
considers `active` policies, so the invariant worth enforcing is "at most one
ACTIVE policy per (org, resolution level)" -- two RETIRED policies at the same
level are history no resolution query looks at. Before 0036 the predicates
omitted `active` and the retirement path documented below was a dead end: a
policy `delete_policy` refuses to delete could be deactivated but never
REPLACED, because the retired row went on blocking its own level, and the
collision message said "an active policy ... already exists" when none was
active. With `AND active` the message is true by construction, and an
INACTIVE policy is outside every arbiter index -- so creating or deactivating
a policy at an already-occupied level never collides, and only making a policy
active at a level another ACTIVE policy already holds does.

DELETE VS RESTRICT (spec §9, Task 4 brief item 5). `ticket_sla.policy_id` is a
plain (RESTRICT) foreign key -- migration 0033 sets no `ondelete`. A
`ticket_sla` row's `policy_id` records what was promised to that ticket at
the time (spec §2.4, §9); rewriting or losing that history because an admin
deleted the policy would destroy its audit value. `delete_policy` therefore
pre-checks for ANY `ticket_sla` row in this org still referencing the policy
and REFUSES deletion outright if one exists, naming the tickets --
mirroring `calendars.delete_calendar`'s `ticket_sla` precheck, which is the
in-repo precedent for this exact FK shape. The caller's only path to retire a
policy that already has history is `update_policy(..., {"active": False})`
(spec §3: `resolve_policy` only ever considers `active` policies) -- this
leaves the row, and every `ticket_sla` row pointing at it, untouched, which is
the literal meaning of "leaves existing ticket_sla rows intact". Because
uniqueness is `active`-scoped (above), deactivating genuinely FREES the level:
a replacement policy can then be created at the same `(queue, priority)`, and
resolution returns the replacement while the retired row and every
`ticket_sla` row pointing at it stay exactly as they were. A policy with NO
`ticket_sla` references can be hard-deleted freely; there is no "active
policy" restriction on policy deletion the way there is on `sla_calendar`
deletion (spec §9 restricts CALENDAR deletion by active POLICY references, not
policy deletion by anything else).

A POLICY EDIT THAT MOVES A DEADLINE RECOMPUTES THE OPEN TICKETS UNDER IT
(spec §7, wired in Task 11). `update_policy` calls
`sweep.recompute_for_policy_change` when -- and only when -- it actually
changed `response_target_minutes`, `resolution_target_minutes` or
`calendar_id`, inside the same transaction as the edit. Those three are the
whole input to `add_working_minutes`; `queue_id`, `priority` and `active`
change which policy FUTURE tickets resolve to (spec §3) and cannot move the
deadline of a row that already points at this one, so they recompute nothing.

That call also decides whether an affected row adopts the policy's possibly-
repointed `calendar_id`. It does, and the argument -- including why it does
not reopen §0.4's door -- is in `recompute_for_policy_change`'s own docstring,
where the behaviour lives. The import is function-local for the same reason
as `calendars.py`'s: `sweep` imports both modules at module scope.

CROSS-ORG FK VALIDATION (mirrors `email_intake.mailboxes._resolve_category`,
referenced by `calendars.py`'s own module docstring as the discipline Task 4
follows). `calendar_id`, `queue_id`, and `escalate_to_queue_id` are all plain
FKs into tables (`sla_calendar`, `ticket_queue`) with no org-aware constraint
of their own -- Postgres alone would happily let this org's policy point at
another org's calendar or queue. `_resolve_calendar`/`_resolve_queue` reject
that at write time as a typed `SlaValidationError`, both on create and on
update.
"""

import logging
from uuid import UUID

from sqlalchemy import exists, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.global_rows import refuse_unless_every_client
from app.core.i18n import tr
from app.models.clients import CLIENT_KIND_UNASSIGNED
from app.modules.service_desk.models import TicketQueue
from app.modules.sla.errors import (
    SlaClientError,
    SlaConflictError,
    SlaNotFoundError,
    SlaValidationError,
)
from app.modules.sla.models import SlaCalendar, SlaPolicy, TicketSla
from app.services.client_sites import ClientNotFound, NotAcceptingNewRows, client_for_new_rows

__all__ = [
    "create_policy",
    "delete_policy",
    "get_policy",
    "list_policies",
    "resolve_policy",
    "update_policy",
]

logger = logging.getLogger("its.sla.policies")

_VALID_PRIORITIES = frozenset({"low", "normal", "high", "urgent"})

# The policy fields that are inputs to `add_working_minutes` and can therefore
# move an ALREADY-materialised deadline (spec §7, see the module docstring).
# `queue_id`/`priority`/`active` are deliberately absent: they select which
# policy a FUTURE ticket resolves to (spec §3), and a row that already points
# at this policy keeps pointing at it whatever they say.
_DEADLINE_INPUT_FIELDS = frozenset(
    {"calendar_id", "response_target_minutes", "resolution_target_minutes"}
)

# The four partial unique indexes (spec §2.4, migrations 0033 and 0036; per
# client since 0231, Task 8b: each holds `client_id` under NULLS NOT DISTINCT,
# so a global policy conflicts only with the global policy at its level, and
# a client's (plan 2026-10-26, D2) only with that client's;
# `models.py::SlaPolicy.__table_args__`), keyed by (has_queue, has_priority)
# -- what `create_policy` uses to pick the right ON CONFLICT target, and what
# `update_policy`'s fallback narrowing matches an `IntegrityError`'s
# `constraint_name` against. `index_elements`/`index_where` must reproduce
# each index's DDL predicate exactly (see module docstring) -- Postgres
# infers the target index from the (columns, predicate) pair, not the name,
# and `AND active` (migration 0036) is part of every predicate.
_POLICY_INDEX_BY_LEVEL: dict[tuple[bool, bool], tuple[str, list[str], str]] = {
    (True, True): (
        "uq_sla_policy_client_queue_and_priority",
        ["org_id", "client_id", "queue_id", "priority"],
        "queue_id IS NOT NULL AND priority IS NOT NULL AND active",
    ),
    (True, False): (
        "uq_sla_policy_client_queue_wildcard_priority",
        ["org_id", "client_id", "queue_id"],
        "queue_id IS NOT NULL AND priority IS NULL AND active",
    ),
    (False, True): (
        "uq_sla_policy_client_wildcard_queue_priority",
        ["org_id", "client_id", "priority"],
        "queue_id IS NULL AND priority IS NOT NULL AND active",
    ),
    (False, False): (
        "uq_sla_policy_client_wildcard_both",
        ["org_id", "client_id"],
        "queue_id IS NULL AND priority IS NULL AND active",
    ),
}
_POLICY_UNIQUE_INDEX_NAMES = frozenset(name for name, _, _ in _POLICY_INDEX_BY_LEVEL.values())

# `ticket_sla.policy_id`'s Postgres-default `<table>_<column>_fkey` name
# (migration 0033 sets no explicit constraint name), confirmed against a live
# database the same way `calendars.py`'s `_CALENDAR_INBOUND_FK_CONSTRAINTS`
# was (Task 3 review F5) -- `delete_policy`'s defence-in-depth fallback below
# uses this to tell "this policy is still referenced" from "something
# unrelated in this flush failed".
_POLICY_INBOUND_FK_CONSTRAINTS = frozenset({"ticket_sla_policy_id_fkey"})

_POLICY_NON_NULLABLE_FIELDS = frozenset(
    {
        "calendar_id",
        "response_target_minutes",
        "resolution_target_minutes",
        "escalate_priority",
        "active",
    }
)
# `queue_id`, `priority`, and `escalate_to_queue_id` are genuinely nullable
# columns used as wildcards (spec §2.4) -- unlike every editable field on
# `calendars.update_calendar`, an explicit `None` here is a real, legal edit
# (e.g. turning a queue-specific policy into a queue-wildcard one), not a
# caller mistake. `update_policy` therefore keys "was this field supplied" on
# `field in fields`, not `fields[field] is not None` -- see the loop below.
_POLICY_EDITABLE_FIELDS = (
    "queue_id",
    "priority",
    "calendar_id",
    "response_target_minutes",
    "resolution_target_minutes",
    "escalate_priority",
    "escalate_to_queue_id",
    "active",
)


def _index_for_level(queue_id: UUID | None, priority: str | None) -> tuple[str, list[str], str]:
    return _POLICY_INDEX_BY_LEVEL[(queue_id is not None, priority is not None)]


def _validate_priority(priority: str | None) -> None:
    """`sla_policy.priority` is constrained to the ticket core's domain by a
    DB CHECK (`ck_sla_policy_priority_valid`) mirroring
    `ck_ticket_origin_valid`'s discipline (spec §2.4). Rejected here as a
    typed 400 first, pre-empting the DB-shaped 500 the same way
    `calendars._validate_timezone` does."""
    if priority is not None and priority not in _VALID_PRIORITIES:
        raise SlaValidationError(
            tr(
                "priority must be one of {priorities} or None; got {priority}",
                priorities=sorted(_VALID_PRIORITIES),
                priority=repr(priority),
            )
        )


def _validate_positive(value: int, field: str) -> None:
    """Both targets are positive-only by DB CHECK (spec §2.4: a zero target
    would be breached at creation, which is never what anyone means).
    Rejected here first for the same reason as `_validate_priority`."""
    if value <= 0:
        raise SlaValidationError(
            tr(
                "{field} must be a positive integer; got {value}",
                field=field,
                value=value,
            )
        )


def _reject_null_for_non_nullable(fields: dict[str, object], non_nullable: frozenset[str]) -> None:
    """A supplied `None` for a NOT NULL column would otherwise reach Postgres
    as a NOT NULL violation and surface as a 500; reject it up front as a
    typed 400 instead (mirrors `calendars.py`'s helper of the same name)."""
    for field in non_nullable:
        if field in fields and fields[field] is None:
            raise SlaValidationError(tr("{field} cannot be null", field=field))


async def _resolve_calendar(session: AsyncSession, org_id: UUID, calendar_id: UUID) -> bool:
    """`calendar_id` must belong to THIS org -- a plain FK does not enforce
    that, so an unvalidated cross-org `calendar_id` would let this org's
    policy point at a calendar it cannot see or administer.

    Returns the calendar's own `active` flag, for `_reject_inactive_calendar`.
    Deliberately selects `active` rather than `id`: `scalar_one_or_none()`
    still returns `None` for "no such row in this org" (and `False`, which is
    not `None`, for a deactivated one), so the cross-org check is unchanged
    and costs no extra query."""
    calendar_active = (
        await session.execute(
            select(SlaCalendar.active).where(
                SlaCalendar.org_id == org_id, SlaCalendar.id == calendar_id
            )
        )
    ).scalar_one_or_none()
    if calendar_active is None:
        raise SlaValidationError(
            tr(
                "calendar_id {calendar_id} does not resolve in this org",
                calendar_id=calendar_id,
            )
        )
    return calendar_active


def _reject_inactive_calendar(
    calendar_id: UUID, *, calendar_active: bool, policy_active: bool
) -> None:
    """An ACTIVE policy may not reference a DEACTIVATED calendar (Task 4
    review F5, decided rather than left ambiguous).

    `sla_calendar.active` was inert on this write path: nothing consulted it,
    so an admin could take a calendar out of service and Task 5's clock would
    still compute every deadline from it, silently. Spec §9 only constrains
    calendar DELETION ("a calendar referenced by an active policy cannot be
    deleted"), but deactivation is the softer half of the same intent --
    `test_sla_migration._seed_calendar`'s own docstring describes
    `active=False` as "a calendar an active policy must not be allowed to
    reference". So it is refused here, symmetrically with
    `calendars.delete_calendar`'s active-policy check, on BOTH create and
    update.

    An INACTIVE policy may reference an inactive calendar freely: a retired
    policy is history (see the module docstring), resolution never returns it,
    and forcing retired rows to be repointed at a live calendar would rewrite
    exactly the history §9 protects. That is also what makes retiring a
    calendar-and-policy pair together possible at all."""
    if policy_active and not calendar_active:
        raise SlaValidationError(
            tr(
                "calendar_id {calendar_id} is not active; an active policy cannot reference "
                "a deactivated calendar",
                calendar_id=calendar_id,
            )
        )


async def _resolve_queue(
    session: AsyncSession, org_id: UUID, queue_id: UUID, *, field: str
) -> None:
    """Same cross-org guard as `_resolve_calendar`, for `queue_id` and
    `escalate_to_queue_id` (both plain FKs into `ticket_queue`)."""
    exists = (
        await session.execute(
            select(TicketQueue.id).where(TicketQueue.org_id == org_id, TicketQueue.id == queue_id)
        )
    ).scalar_one_or_none()
    if exists is None:
        raise SlaValidationError(
            tr(
                "{field} {queue_id} does not resolve in this org",
                field=field,
                queue_id=queue_id,
            )
        )


async def get_policy(session: AsyncSession, org_id: UUID, policy_id: UUID) -> SlaPolicy:
    """Org-scoped load. A policy id from another org is indistinguishable
    from a nonexistent one -- both raise `SlaNotFoundError` (invariant #5)."""
    policy = (
        await session.execute(
            select(SlaPolicy).where(SlaPolicy.org_id == org_id, SlaPolicy.id == policy_id)
        )
    ).scalar_one_or_none()
    if policy is None:
        raise SlaNotFoundError(tr("policy {policy_id} not found", policy_id=policy_id))
    return policy


async def list_policies(session: AsyncSession, org_id: UUID) -> list[SlaPolicy]:
    return list(
        (
            await session.execute(
                select(SlaPolicy).where(SlaPolicy.org_id == org_id).order_by(SlaPolicy.created_at)
            )
        )
        .scalars()
        .all()
    )


async def create_policy(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID,
    *,
    queue_id: UUID | None = None,
    priority: str | None = None,
    calendar_id: UUID,
    response_target_minutes: int,
    resolution_target_minutes: int,
    escalate_priority: bool = False,
    escalate_to_queue_id: UUID | None = None,
    active: bool = True,
    client_id: UUID | None = None,
) -> SlaPolicy:
    """`ON CONFLICT DO NOTHING` + `RETURNING id` against the ONE partial
    unique index matching this policy's resolution level (see module
    docstring for why `index_elements`/`index_where`, not `constraint=`) --
    not check-then-insert. The race loser gets the SAME typed
    `SlaValidationError` a pre-check would raise, and never the winner's
    row.

    Each arbiter predicate ends `AND active` (migration 0036), so an
    `active=False` policy is outside every index and can always be created:
    creating a retired policy at a level an active one already holds is legal
    history, and the collision below can only ever mean a genuinely ACTIVE
    policy is in the way -- which is what its message says.

    `client_id` makes the policy that client's (MSP clients plan 2026-10-26,
    D2): its tickets then resolve among its own policies alone. The client is
    loaded as every new row's is (`client_for_new_rows`, held FOR SHARE until
    commit): one the session cannot see is a 404, an archived one a 409, and
    "Sin asignar" -- the inbox of what has no owner, not a client anyone
    configures (C10) -- a 422. A client's policy and a global one may share a
    level: each of the four keys holds `client_id` (0231)."""
    if client_id is None:
        # A global row, every client's: the bulk INSERT below never passes
        # `app.core.global_rows`'s flush guard, so it is refused first, as
        # `require_all_clients` is -- a limited technician learns nothing
        # from a validation error about a body they could never have sent.
        refuse_unless_every_client(session, "sla_policy")
    else:
        try:
            client = await client_for_new_rows(session, org_id=org_id, client_id=client_id)
        except ClientNotFound:
            raise SlaNotFoundError(tr("no such client")) from None
        except NotAcceptingNewRows as exc:
            raise SlaConflictError(exc.detail) from None
        if client.kind == CLIENT_KIND_UNASSIGNED:
            raise SlaClientError(tr("Unassigned has no SLA policies of its own"))
    _validate_priority(priority)
    _validate_positive(response_target_minutes, "response_target_minutes")
    _validate_positive(resolution_target_minutes, "resolution_target_minutes")
    calendar_active = await _resolve_calendar(session, org_id, calendar_id)
    _reject_inactive_calendar(calendar_id, calendar_active=calendar_active, policy_active=active)
    if queue_id is not None:
        await _resolve_queue(session, org_id, queue_id, field="queue_id")
    if escalate_to_queue_id is not None:
        await _resolve_queue(session, org_id, escalate_to_queue_id, field="escalate_to_queue_id")

    _, index_elements, index_where = _index_for_level(queue_id, priority)

    result = await session.execute(
        pg_insert(SlaPolicy)
        .values(
            org_id=org_id,
            client_id=client_id,
            queue_id=queue_id,
            priority=priority,
            calendar_id=calendar_id,
            response_target_minutes=response_target_minutes,
            resolution_target_minutes=resolution_target_minutes,
            escalate_priority=escalate_priority,
            escalate_to_queue_id=escalate_to_queue_id,
            active=active,
            created_by=actor_id,
        )
        .on_conflict_do_nothing(index_elements=index_elements, index_where=text(index_where))
        .returning(SlaPolicy.id)
    )
    inserted_id = result.scalar_one_or_none()
    if inserted_id is None:
        raise SlaValidationError(
            tr(
                "an active policy for (queue_id={queue_id}, priority={priority}) already "
                "exists in this org",
                queue_id=queue_id,
                priority=repr(priority),
            )
        )

    policy = await get_policy(session, org_id, inserted_id)

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="sla.policy_created",
        target_type="sla_policy",
        target_id=str(policy.id),
        client_id=client_id,
        metadata={
            "queue_id": str(queue_id) if queue_id else None,
            "priority": priority,
            "calendar_id": str(calendar_id),
            "response_target_minutes": response_target_minutes,
            "resolution_target_minutes": resolution_target_minutes,
            "escalate_priority": escalate_priority,
            "escalate_to_queue_id": str(escalate_to_queue_id) if escalate_to_queue_id else None,
            "active": active,
        },
    )
    await session.flush()
    return policy


async def update_policy(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID,
    policy_id: UUID,
    fields: dict[str, object],
) -> SlaPolicy:
    """Apply the SUPPLIED fields to a policy, org-scoped-loaded. `queue_id`,
    `priority`, and `escalate_to_queue_id` may be legally set to `None`
    (wildcarding a level) -- presence in `fields`, not truthiness, decides
    whether a field is touched (see `_POLICY_EDITABLE_FIELDS`'s comment).

    A change that collides with another ACTIVE policy at the same resolution
    level hits whichever of the four partial unique indexes matches
    (`_POLICY_UNIQUE_INDEX_NAMES`) and is converted to a typed
    `SlaValidationError`, never a raw `IntegrityError` -- the flush runs
    inside a SAVEPOINT (`session.begin_nested()`), opened BEFORE the row is
    mutated, mirroring `calendars.update_calendar`'s rename-collision
    handling. Because the indexes are `active`-scoped (migration 0036),
    `{"active": False}` can never collide, which is what makes it a usable
    retirement path (module docstring).

    A key that is not in `_POLICY_EDITABLE_FIELDS` is REFUSED, not ignored
    (Task 4 review F6): silently dropping it returned 200 for a typo'd body
    having changed nothing, and audited a no-op edit. The whitelist still
    decides what may be written -- `org_id`, `created_by` and friends remain
    unreachable -- this only changes an unknown key from "quietly discarded"
    to "typed 400"."""
    policy = await get_policy(session, org_id, policy_id)

    unknown = sorted(set(fields) - set(_POLICY_EDITABLE_FIELDS))
    if unknown:
        if len(unknown) == 1:
            unknown_message = tr(
                "unknown policy field: {unknown}; editable fields are {editable}",
                unknown=", ".join(unknown),
                editable=", ".join(_POLICY_EDITABLE_FIELDS),
            )
        else:
            unknown_message = tr(
                "unknown policy fields: {unknown}; editable fields are {editable}",
                unknown=", ".join(unknown),
                editable=", ".join(_POLICY_EDITABLE_FIELDS),
            )
        raise SlaValidationError(unknown_message)

    _reject_null_for_non_nullable(fields, _POLICY_NON_NULLABLE_FIELDS)

    # The COERCED values actually written (Task 4 review F7). Validation used
    # to coerce (`int(...)`) while the assignment loop below assigned
    # `fields[field]` raw, so `{"response_target_minutes": "60"}` validated as
    # 60 and stored the string -- Postgres coerced it on write, but the
    # in-memory attribute, the `!=` change-detection below (which then
    # reported an identical value as "changed"), and the returned object all
    # disagreed with the column. Validate and coerce once, then write what was
    # validated.
    applied: dict[str, object] = dict(fields)
    if "priority" in fields and fields["priority"] is not None:
        priority_value = str(fields["priority"])
        _validate_priority(priority_value)
        applied["priority"] = priority_value
    for minutes_field in ("response_target_minutes", "resolution_target_minutes"):
        if minutes_field in fields and fields[minutes_field] is not None:
            minutes_value = int(fields[minutes_field])  # type: ignore[call-overload]
            _validate_positive(minutes_value, minutes_field)
            applied[minutes_field] = minutes_value

    # `calendar_id`/`active` are checked together: reactivating a policy onto
    # a calendar that was deactivated in the meantime is the same defect as
    # pointing an active policy at a dead calendar in the first place
    # (`_reject_inactive_calendar`). Either key being supplied is enough to
    # need the check, so the effective pair is resolved rather than just the
    # supplied one. `calendar_id` is non-nullable and `_reject_null_for_non_nullable`
    # has already run, so a present `calendar_id` is never None here.
    effective_active = bool(fields["active"]) if "active" in fields else policy.active
    effective_calendar_id = fields["calendar_id"] if "calendar_id" in fields else policy.calendar_id
    if "calendar_id" in fields or "active" in fields:
        calendar_active = await _resolve_calendar(session, org_id, effective_calendar_id)  # type: ignore[arg-type]
        _reject_inactive_calendar(
            effective_calendar_id,  # type: ignore[arg-type]
            calendar_active=calendar_active,
            policy_active=effective_active,
        )
    if "queue_id" in fields and fields["queue_id"] is not None:
        await _resolve_queue(session, org_id, fields["queue_id"], field="queue_id")  # type: ignore[arg-type]
    if "escalate_to_queue_id" in fields and fields["escalate_to_queue_id"] is not None:
        await _resolve_queue(
            session, org_id, fields["escalate_to_queue_id"], field="escalate_to_queue_id"  # type: ignore[arg-type]
        )

    # Computed BEFORE the mutating SAVEPOINT below, from `applied` and the
    # pre-update row -- never from `policy.queue_id`/`policy.priority` AFTER
    # a failed flush, whose in-memory value is no longer reliably the
    # pre-update one once the SAVEPOINT starts rolling back (mirrors
    # `calendars.update_calendar` reading `fields['name']`, not
    # `calendar.name`, in its own collision message).
    effective_queue_id = applied["queue_id"] if "queue_id" in applied else policy.queue_id
    effective_priority = applied["priority"] if "priority" in applied else policy.priority

    changed: list[str] = []
    try:
        async with session.begin_nested():
            for field in _POLICY_EDITABLE_FIELDS:
                if field not in applied:
                    continue
                value = applied[field]
                if getattr(policy, field) != value:
                    setattr(policy, field, value)
                    changed.append(field)
            session.add(policy)
            await session.flush()
    except IntegrityError as exc:
        constraint = getattr(getattr(exc.orig, "__cause__", None), "constraint_name", None)
        if constraint not in _POLICY_UNIQUE_INDEX_NAMES:
            raise
        raise SlaValidationError(
            tr(
                "an active policy for (queue_id={effective_queue_id}, "
                "priority={effective_priority}) already exists in this org",
                effective_queue_id=effective_queue_id,
                effective_priority=repr(effective_priority),
            )
        ) from exc

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="sla.policy_updated",
        target_type="sla_policy",
        target_id=str(policy.id),
        client_id=policy.client_id,
        metadata={"changed": sorted(changed)},
    )
    await session.flush()
    if set(changed) & _DEADLINE_INPUT_FIELDS:
        # Function-local: `sweep` imports this module's siblings and would
        # cycle. See the module docstring.
        from app.modules.sla.sweep import recompute_for_policy_change

        counts = await recompute_for_policy_change(session, org_id, policy.id)
        log = logger.warning if counts["failed"] else logger.info
        log(
            "sla.policy %s changed (%s) in org %s: recomputed=%d skipped=%d failed=%d",
            policy.id,
            ", ".join(sorted(set(changed) & _DEADLINE_INPUT_FIELDS)),
            org_id,
            counts["recomputed"],
            counts["skipped"],
            counts["failed"],
        )
    return policy


async def delete_policy(
    session: AsyncSession, org_id: UUID, actor_id: UUID, policy_id: UUID
) -> None:
    """Refuses if ANY `ticket_sla` row in this org still references the
    policy, naming the tickets, and never touches them otherwise (spec §9,
    Task 4 brief item 5) -- see module docstring for the RESTRICT-FK
    reasoning and why deactivation (`update_policy(..., {"active": False})`)
    is the intended path for a policy with history. Since migration 0036 that
    path is complete rather than a dead end: the four unique indexes are
    `active`-scoped, so retiring a policy frees its `(queue, priority)` level
    and a replacement can be created there immediately (Task 4 review F2).
    A policy with no `ticket_sla` references is deleted outright."""
    policy = await get_policy(session, org_id, policy_id)

    ticket_ids = (
        await session.execute(
            select(TicketSla.ticket_id).where(
                TicketSla.org_id == org_id, TicketSla.policy_id == policy_id
            )
        )
    ).scalars().all()
    if ticket_ids:
        ids = sorted(str(t) for t in ticket_ids)
        if len(ids) == 1:
            ticket_message = tr(
                "policy {policy_id} cannot be deleted: an SLA record exists for "
                "ticket {ids} under this policy; set active=False instead to retire it -- "
                "that preserves their history and frees this (queue, priority) level for a "
                "replacement policy",
                policy_id=policy_id,
                ids=", ".join(ids),
            )
        else:
            ticket_message = tr(
                "policy {policy_id} cannot be deleted: an SLA record exists for "
                "tickets {ids} under this policy; set active=False instead to retire it -- "
                "that preserves their history and frees this (queue, priority) level for a "
                "replacement policy",
                policy_id=policy_id,
                ids=", ".join(ids),
            )
        raise SlaValidationError(ticket_message)

    target_id = str(policy.id)
    policy_client = policy.client_id
    metadata = {
        "queue_id": str(policy.queue_id) if policy.queue_id else None,
        "priority": policy.priority,
    }
    await session.delete(policy)
    try:
        await session.flush()
    except IntegrityError as exc:
        # Defence in depth, not the primary guard -- mirrors
        # `calendars.delete_calendar`'s identically-shaped fallback (see its
        # docstring's "FALLBACK NARROWING" section for the full reasoning).
        # The org-scoped precheck above cannot see a rogue cross-org
        # `ticket_sla` row (the FK has no org concept), and this flush can
        # also emit any OTHER pending state in the caller's unit of work --
        # only convert when the violated constraint is actually
        # `ticket_sla_policy_id_fkey`; everything else re-raises un-mangled.
        constraint = getattr(getattr(exc.orig, "__cause__", None), "constraint_name", None)
        if constraint not in _POLICY_INBOUND_FK_CONSTRAINTS:
            raise
        raise SlaValidationError(
            tr(
                "policy {policy_id} cannot be deleted: it is still referenced by another "
                "record this organization cannot see",
                policy_id=policy_id,
            )
        ) from exc

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="sla.policy_deleted",
        target_type="sla_policy",
        target_id=target_id,
        client_id=policy_client,
        metadata=metadata,
    )
    await session.flush()


async def resolve_policy(
    session: AsyncSession,
    org_id: UUID,
    *,
    client_id: UUID,
    queue_id: UUID | None,
    priority: str | None,
) -> SlaPolicy | None:
    """Spec §3's resolution order, exactly: most specific first --

    1. `(queue_id, priority)` 2. `(queue_id, NULL)` 3. `(NULL, priority)`
    4. `(NULL, NULL)`

    -- among ONE owner's policies (MSP clients plan 2026-10-26, D2): the
    ticket's client's, when it has any active policy of its own; otherwise the
    global ones (`client_id` NULL). A client's own policies replace the global
    ones whole, as a contract does; the two are never mixed level by level.

    THE CLIENT PREDICATE IS EXPLICIT. The email poller and the SLA sweep run
    under scope all, where RLS shows every client's policies: without it a
    global row and a client row at one level raise `MultipleResultsFound`
    (research-1: a 500 on create and a wedged mailbox), and another client's
    lone policy would be applied to this ticket. The per-client keys of 0231
    leave one active row per level and owner, so `scalar_one_or_none` is safe
    again.

    Only `active` policies are considered -- for the owner too: a client whose
    only policies are retired is back on the global ones. Returns `None` when
    nothing matches -- NEVER a fabricated default, never a zero. Spec §3: an
    org with no policy configured must visibly have no SLA, because an
    invented target is worse than an absent one. No `ticket_sla` row is
    created by this function or because of its `None` return -- that decision
    belongs entirely to the caller (Task 5)."""
    has_own = (
        await session.execute(
            select(
                exists().where(
                    SlaPolicy.org_id == org_id,
                    SlaPolicy.client_id == client_id,
                    SlaPolicy.active.is_(True),
                )
            )
        )
    ).scalar_one()
    owner = SlaPolicy.client_id == client_id if has_own else SlaPolicy.client_id.is_(None)
    levels = [(queue_id, priority), (queue_id, None), (None, priority), (None, None)]
    seen: set[tuple[UUID | None, str | None]] = set()
    for level_queue_id, level_priority in levels:
        # De-duplicates a level that already collapsed to a level below it
        # (e.g. a caller passing `queue_id=None` up front makes levels 1/2
        # and 3/4 identical pairs) -- purely to skip a redundant, harmless
        # repeat query; it changes nothing about which level wins.
        if (level_queue_id, level_priority) in seen:
            continue
        seen.add((level_queue_id, level_priority))

        conditions = [SlaPolicy.org_id == org_id, SlaPolicy.active.is_(True), owner]
        conditions.append(
            SlaPolicy.queue_id == level_queue_id
            if level_queue_id is not None
            else SlaPolicy.queue_id.is_(None)
        )
        conditions.append(
            SlaPolicy.priority == level_priority
            if level_priority is not None
            else SlaPolicy.priority.is_(None)
        )
        policy = (await session.execute(select(SlaPolicy).where(*conditions))).scalar_one_or_none()
        if policy is not None:
            return policy
    return None
