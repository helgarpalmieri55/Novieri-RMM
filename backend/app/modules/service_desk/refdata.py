"""Reference data: ticket queues and categories
(docs/modules/service-desk/spec.md §2, §5).

Mirrors `app.modules.budget.refdata`'s style closely -- it solved every
problem here already: the audited system-actor lazy seed, `pg_insert(...)
.on_conflict_do_nothing()` for concurrency-safe duplicate handling, and
org-scoped resolvers that make a cross-org id indistinguishable from a
nonexistent one (invariant #5).

Every mutation here calls `audit_service.record` and then flushes
(invariant #4); every query carries an explicit `org_id` predicate
(invariant #5).

**`create_category`/`update_category` resolve BOTH `default_queue_id` and
`approver_role_id` org-scoped, via `resolve_queue` and `resolve_role`
respectively (whole-branch security review, finding A2).** An earlier
revision passed both straight into the write with no resolution at all --
the ONLY two unresolved cross-refs in this module, every other one
(`resolve_category`, `resolve_queue`, and `tickets.resolve_user`/
`_resolve_device`) was already correctly scoped. **Postgres FK checks always
bypass row security** -- RLS does not save an unresolved FK the way it saves
a plain `SELECT` -- so a bare `approver_role_id`/`default_queue_id` value
from another org would pass the `roles.id`/`ticket_queue.id` FK just fine and
land on this org's category. The consequences were not cosmetic: a cross-org
`approver_role_id` can never be satisfied by anyone in this org, and
`lifecycle.py` gives `pending_approval` no exit but `decide_approval`, so
every request in that category is stuck forever; a cross-org
`default_queue_id` makes every ticket creation in that category 404
(`create_ticket`'s own `resolve_queue` call, correctly org-scoped, rejects
it); and worst, an org could use this as a cross-tenant EXISTENCE ORACLE --
a uuid that exists in another org returned 201 (and then `CategoryOut` served
that foreign org's uuid straight back), one that exists nowhere 500'd on an
unhandled `IntegrityError`. Resolving both through the SAME org-scoped
resolvers every other cross-ref in this module already uses closes all of
that: a cross-org or nonexistent id now raises the identical
`TicketNotFoundError` (a 404), indistinguishable, before anything is
written.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from uuid_extensions import uuid7

from app.core.audit import audit_service
from app.core.i18n import tr
from app.models import Role, TicketCategory, TicketQueue
from app.modules.service_desk.errors import TicketNotFoundError, TicketValidationError

DEFAULT_QUEUES: tuple[tuple[str, str], ...] = (
    ("helpdesk", "Helpdesk"),
    ("infrastructure", "Infrastructure"),
    ("security", "Security"),
)

_DEFAULT_QUEUE_KEYS = tuple(key for key, _name in DEFAULT_QUEUES)

# (key, name, type, requires_approval, default_queue_key) -- verbatim from
# docs/modules/service-desk/spec.md §2's `ticket_category` seed table (added
# after Task 6: `ticket.category_id` is NOT NULL, so an org with zero
# categories has no legal ticket to create). `access_request` and
# `equipment_request` require approval -- "grant me access" and "buy me a
# laptop" are the canonical cases §4 exists for, so a fresh org exercises the
# approval path without configuration, while a software install or a general
# request does not sit behind a signature. `default_queue_key` is resolved
# against `DEFAULT_QUEUES`' rows by `ensure_default_categories`, never a raw
# id -- the two seeds must stay in lockstep.
DEFAULT_CATEGORIES: tuple[tuple[str, str, str, bool, str], ...] = (
    ("hardware", "Hardware", "incident", False, "helpdesk"),
    ("software", "Software", "incident", False, "helpdesk"),
    ("network", "Network", "incident", False, "infrastructure"),
    ("access", "Access", "incident", False, "security"),
    ("other", "Other", "incident", False, "helpdesk"),
    ("access_request", "Access request", "request", True, "security"),
    ("equipment_request", "Equipment request", "request", True, "helpdesk"),
    ("software_request", "Software request", "request", False, "helpdesk"),
    ("other_request", "Other request", "request", False, "helpdesk"),
)

_DEFAULT_CATEGORY_KEYS = tuple(key for key, _name, _type, _req, _queue_key in DEFAULT_CATEGORIES)

# Fields `update_queue`/`update_category` apply as plain scalar assignment,
# decided by key PRESENCE in `fields` (not by the value being non-None) --
# callers pass `model_dump(exclude_unset=True)`, so a key is present only if
# actually supplied. `key` is deliberately absent from both editable sets --
# it is the stable identifier a duplicate-key check and any future resolver
# key off of, and it must never move under a rename.
_QUEUE_EDITABLE_FIELDS = ("name", "description", "active", "sort_order")
_QUEUE_NON_NULLABLE_FIELDS = frozenset({"name", "active", "sort_order"})

_CATEGORY_EDITABLE_FIELDS = (
    "name",
    "requires_approval",
    "approver_role_id",
    "default_queue_id",
    "active",
    "sort_order",
)
_CATEGORY_NON_NULLABLE_FIELDS = frozenset({"name", "requires_approval", "active", "sort_order"})


def _reject_null_for_non_nullable(
    fields: dict[str, Any], non_nullable: frozenset[str]
) -> None:
    """A supplied `None` for a NOT NULL column would otherwise reach Postgres
    as a NOT NULL violation and surface as a 500; reject it up front as a
    typed 400 instead (the `budget.refdata` pattern)."""
    for field in non_nullable:
        if field in fields and fields[field] is None:
            raise TicketValidationError(tr("{field} cannot be null", field=field))


def _reject_incident_requires_approval(ticket_type: str, requires_approval: bool) -> None:
    """Spec §2: `requires_approval` is meaningful only for `type="request"`.
    The DB pins the same rule with `ck_category_approval_is_request_only`,
    but rejecting it here first means the caller gets a typed 400 instead of
    a raw `IntegrityError` surfacing as a 500."""
    if ticket_type == "incident" and requires_approval:
        raise TicketValidationError(tr("requires_approval cannot be set on an incident category"))


# ---------------------------------------------------------------------------
# ticket_queue
# ---------------------------------------------------------------------------


async def ensure_default_queues(
    session: AsyncSession, org_id: UUID, actor_id: UUID
) -> list[TicketQueue]:
    """Ensure the org has all of `DEFAULT_QUEUES`, inserting only the ones it
    is missing. Idempotent BY CONSTRUCTION -- the org's existing queues are
    selected first, so a second call for the same org inserts nothing.

    This is a LAZY seed, called from `list_queues` (and any other path that
    needs queues to exist) rather than from a one-time org-bootstrap hook --
    there is no such hook anywhere in the platform (mirrors
    `budget.refdata.seed_default_categories`).

    The existence check covers `key`, which is what `uq_ticket_queue_org_key`
    is on. An operator who created a queue named e.g. "helpdesk" by hand
    before this ever ran is SKIPPED, not adopted -- it keeps its own row and
    the seed never collides with it.

    Concurrency-safe: two concurrent first-ever calls for the same org (e.g.
    two requests racing `list_queues` on a brand-new org) could both miss the
    in-memory pre-check before either has written. The insert is a single
    batched `pg_insert(...).on_conflict_do_nothing()` against
    `uq_ticket_queue_org_key` rather than per-row `session.add` calls, which
    would let the loser of that race surface a raw `IntegrityError` as a 500;
    `RETURNING` reports which rows actually landed, so the loser of a race is
    silently skipped rather than double-inserted or errored.

    Audit attribution: this function is reachable from every `ticket.read`
    path (`list_queues`), including a caller who holds only `ticket.read`.
    Emitting the operator-facing `ticket.queue_created` event here would
    attribute a bootstrap the reader never asked for to that reader in the
    immutable hash-chained log, as if they had managed the org's queues
    (`ticket.admin`'s job). So a seed that actually inserts something writes
    exactly ONE system-attributed event, `ticket.queues_seeded`
    (`actor_type="system"`, `actor_id=None`), naming the keys it inserted; a
    call that inserts nothing (the common case after the first) writes
    nothing at all. This was a real defect in Budget's first cut: per-row
    user-attributed events on a lazy seed made a read-only user, in the
    immutable audit log, the creator of the org's reference data."""
    existing_keys = set(
        (
            await session.execute(
                select(TicketQueue.key).where(
                    TicketQueue.org_id == org_id,
                    TicketQueue.key.in_(_DEFAULT_QUEUE_KEYS),
                )
            )
        )
        .scalars()
        .all()
    )

    missing = [(key, name) for key, name in DEFAULT_QUEUES if key not in existing_keys]
    if missing:
        rows = [
            {
                "id": uuid7(),
                "org_id": org_id,
                "key": key,
                "name": name,
                "created_by": actor_id,
            }
            for key, name in missing
        ]
        inserted = (
            await session.execute(
                pg_insert(TicketQueue)
                .values(rows)
                .on_conflict_do_nothing(index_elements=["org_id", "key"])
                .returning(TicketQueue.id, TicketQueue.key)
            )
        ).all()
        await session.flush()

        if inserted:
            await audit_service.record(
                session,
                org_id=org_id,
                actor_type="system",
                actor_id=None,
                action="ticket.queues_seeded",
                target_type="org",
                target_id=str(org_id),
                metadata={"keys": [key for _queue_id, key in inserted]},
            )
            await session.flush()

    return list(
        (
            await session.execute(
                select(TicketQueue)
                .where(
                    TicketQueue.org_id == org_id,
                    TicketQueue.key.in_(_DEFAULT_QUEUE_KEYS),
                )
                .order_by(TicketQueue.sort_order, TicketQueue.name)
            )
        )
        .scalars()
        .all()
    )


async def list_queues(
    session: AsyncSession, org_id: UUID, *, include_inactive: bool = False
) -> list[TicketQueue]:
    """Plain org-scoped list -- deliberately does NOT invoke
    `ensure_default_queues` itself (unlike `budget.refdata.list_categories`,
    which can because its equivalent seed takes the SAME `actor_id` the list
    call already carries). This function has no `actor_id` parameter, so it
    cannot supply one to the seed's NOT-NULL `created_by`; the caller (the
    API route, from `AuthContext`) calls `ensure_default_queues` explicitly
    when it needs the org bootstrapped, then lists."""
    stmt = select(TicketQueue).where(TicketQueue.org_id == org_id)
    if not include_inactive:
        stmt = stmt.where(TicketQueue.active.is_(True))
    stmt = stmt.order_by(TicketQueue.sort_order, TicketQueue.name)
    return list((await session.execute(stmt)).scalars().all())


async def create_queue(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID,
    *,
    key: str,
    name: str,
    description: str = "",
    sort_order: int = 0,
) -> TicketQueue:
    """Create an operator queue. A duplicate `key` within the org is rejected
    up front as a typed `TicketValidationError` rather than surfacing the
    `uq_ticket_queue_org_key` violation as a 500; the same `key` in a
    different org is unaffected.

    The pre-check above is a fast path, not the sole guard: two concurrent
    `create_queue` calls for the same `(org_id, key)` can both miss it before
    either has written. A plain `session.add` here would let the loser
    surface a raw `IntegrityError` as a 500, so the actual insert instead
    goes through `ON CONFLICT DO NOTHING` against `uq_ticket_queue_org_key`
    -- but this is a single-caller CREATE, not a get-or-create: a race loser
    must get the identical typed `TicketValidationError` the pre-check
    raises, never someone else's row.

    `sort_order` is an INSERT-time value, not a follow-up `update_queue` call
    (whole-branch security review, minor finding): the route used to create
    then immediately PATCH when a caller supplied a non-default
    `sort_order`, which wrote a spurious `ticket.queue_updated` audit event
    right after `ticket.queue_created` for a single create request. One
    create request now writes exactly one audit event."""
    existing = (
        await session.execute(
            select(TicketQueue.id).where(TicketQueue.org_id == org_id, TicketQueue.key == key)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise TicketValidationError(tr("queue key already exists: {key}", key=key))

    queue_id = uuid7()
    inserted_id = (
        await session.execute(
            pg_insert(TicketQueue)
            .values(
                id=queue_id,
                org_id=org_id,
                key=key,
                name=name,
                description=description,
                sort_order=sort_order,
                created_by=actor_id,
            )
            .on_conflict_do_nothing(index_elements=["org_id", "key"])
            .returning(TicketQueue.id)
        )
    ).scalar_one_or_none()
    if inserted_id is None:
        raise TicketValidationError(tr("queue key already exists: {key}", key=key))

    queue = (
        await session.execute(
            select(TicketQueue).where(TicketQueue.org_id == org_id, TicketQueue.id == inserted_id)
        )
    ).scalar_one()
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="ticket.queue_created",
        target_type="ticket_queue",
        target_id=str(queue.id),
        metadata={"key": key, "name": name},
    )
    await session.flush()
    return queue


async def resolve_queue(session: AsyncSession, org_id: UUID, queue_id: UUID) -> TicketQueue:
    """Org-scoped load. A queue id from another org is indistinguishable
    from a nonexistent one -- both raise `TicketNotFoundError` (spec §8,
    invariant #5)."""
    queue = (
        await session.execute(
            select(TicketQueue).where(TicketQueue.org_id == org_id, TicketQueue.id == queue_id)
        )
    ).scalar_one_or_none()
    if queue is None:
        raise TicketNotFoundError(tr("ticket queue {queue_id} not found", queue_id=queue_id))
    return queue


async def update_queue(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID,
    queue_id: UUID,
    fields: dict[str, Any],
) -> TicketQueue:
    """Apply the SUPPLIED fields to a queue, org-scoped-loaded. `key` is
    never touched here even if present in `fields` (it is absent from
    `_QUEUE_EDITABLE_FIELDS`)."""
    _reject_null_for_non_nullable(fields, _QUEUE_NON_NULLABLE_FIELDS)
    queue = await resolve_queue(session, org_id, queue_id)

    changed_fields = []
    for field in _QUEUE_EDITABLE_FIELDS:
        if field not in fields:
            continue
        if getattr(queue, field) != fields[field]:
            changed_fields.append(field)
        setattr(queue, field, fields[field])
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="ticket.queue_updated",
        target_type="ticket_queue",
        target_id=str(queue.id),
        metadata={"changed_fields": sorted(changed_fields)},
    )
    await session.flush()
    return queue


# ---------------------------------------------------------------------------
# ticket_category
# ---------------------------------------------------------------------------


async def ensure_default_categories(
    session: AsyncSession, org_id: UUID, actor_id: UUID
) -> list[TicketCategory]:
    """Ensure the org has all of `DEFAULT_CATEGORIES`, inserting only the
    ones it is missing. Same mechanics as `ensure_default_queues`: idempotent
    by construction (the org's existing keys are selected first), a single
    batched `pg_insert(...).on_conflict_do_nothing()` against
    `uq_ticket_category_org_key` for concurrency safety, and exactly ONE
    system-attributed `ticket.categories_seeded` audit event
    (`actor_type="system"`, `actor_id=None`) naming the keys actually
    inserted -- nothing at all when nothing is inserted. `created_by` on each
    row is still the acting user who tripped the bootstrap; only the AUDIT
    event's actor is system, for the same reason the queue seed's is: a
    caller holding only `ticket.read` must not be attributed, in the
    immutable log, as the creator of the org's reference data.

    `default_queue_id` resolves against the queues (spec §2), so this calls
    `ensure_default_queues` FIRST rather than assuming it has already run --
    a brand-new org calling only the category seed must end up with both.
    That call is itself idempotent, so an org that already has its queues
    (the common case) pays only the cheap existence check, not a second
    seed."""
    queues = await ensure_default_queues(session, org_id, actor_id)
    queue_id_by_key = {queue.key: queue.id for queue in queues}

    existing_keys = set(
        (
            await session.execute(
                select(TicketCategory.key).where(
                    TicketCategory.org_id == org_id,
                    TicketCategory.key.in_(_DEFAULT_CATEGORY_KEYS),
                )
            )
        )
        .scalars()
        .all()
    )

    missing = [
        (key, name, type_, requires_approval, queue_key)
        for key, name, type_, requires_approval, queue_key in DEFAULT_CATEGORIES
        if key not in existing_keys
    ]
    if missing:
        rows = [
            {
                "id": uuid7(),
                "org_id": org_id,
                "key": key,
                "name": name,
                "type": type_,
                "requires_approval": requires_approval,
                "default_queue_id": queue_id_by_key[queue_key],
                "created_by": actor_id,
            }
            for key, name, type_, requires_approval, queue_key in missing
        ]
        inserted = (
            await session.execute(
                pg_insert(TicketCategory)
                .values(rows)
                .on_conflict_do_nothing(index_elements=["org_id", "key"])
                .returning(TicketCategory.id, TicketCategory.key)
            )
        ).all()
        await session.flush()

        if inserted:
            await audit_service.record(
                session,
                org_id=org_id,
                actor_type="system",
                actor_id=None,
                action="ticket.categories_seeded",
                target_type="org",
                target_id=str(org_id),
                metadata={"keys": [key for _category_id, key in inserted]},
            )
            await session.flush()

    return list(
        (
            await session.execute(
                select(TicketCategory)
                .where(
                    TicketCategory.org_id == org_id,
                    TicketCategory.key.in_(_DEFAULT_CATEGORY_KEYS),
                )
                # `name` as the tie-break, matching `ensure_default_queues` and
                # both `list_*` functions. One file should not order the same
                # kind of thing two different ways.
                .order_by(TicketCategory.sort_order, TicketCategory.name)
            )
        )
        .scalars()
        .all()
    )


async def list_categories(
    session: AsyncSession, org_id: UUID, *, include_inactive: bool = False
) -> list[TicketCategory]:
    stmt = select(TicketCategory).where(TicketCategory.org_id == org_id)
    if not include_inactive:
        stmt = stmt.where(TicketCategory.active.is_(True))
    stmt = stmt.order_by(TicketCategory.sort_order, TicketCategory.name)
    return list((await session.execute(stmt)).scalars().all())


async def resolve_role(session: AsyncSession, org_id: UUID, role_id: UUID) -> Role:
    """Org-scoped load for a category's `approver_role_id` (spec §2, §4;
    whole-branch security review, finding A2).

    `Role.org_id` is NULLABLE: a role is either GLOBAL (`org_id IS NULL` --
    the platform roles seeded by `migrations/versions/0003_rbac_seed.py`:
    `super-admin`, `org-admin`, `technician`, `auditor`, `read-only`, every
    one of them usable by every org) or scoped to one specific org. A role
    is usable as this org's `approver_role_id` if it is global OR belongs to
    THIS org -- a role scoped to a DIFFERENT org is indistinguishable from a
    nonexistent one, both raising `TicketNotFoundError` (spec §8, invariant
    #5), exactly like every other cross-ref resolver in this module.

    This is precisely why the FK to `roles.id` alone can never guarantee
    "no cross-org role id": the FK is satisfied by ANY row in `roles`,
    including one scoped to a different org entirely, and Postgres FK checks
    always bypass row security -- RLS gives no protection here at all."""
    role = (
        await session.execute(
            select(Role).where(
                Role.id == role_id, or_(Role.org_id == org_id, Role.org_id.is_(None))
            )
        )
    ).scalar_one_or_none()
    if role is None:
        raise TicketNotFoundError(tr("role {role_id} not found", role_id=role_id))
    return role


async def create_category(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID,
    *,
    key: str,
    name: str,
    type: str,
    requires_approval: bool = False,
    approver_role_id: UUID | None = None,
    default_queue_id: UUID | None = None,
    sort_order: int = 0,
) -> TicketCategory:
    """Create a category. `type="incident"` with `requires_approval=True` is
    rejected up front as a typed `TicketValidationError` (spec §2) -- the DB
    also pins this with `ck_category_approval_is_request_only`, but the
    service layer must reject it first so the caller gets a 400 rather than
    a raw `IntegrityError` surfacing as a 500.

    `approver_role_id`/`default_queue_id`, when supplied, are resolved
    org-scoped via `resolve_role`/`resolve_queue` BEFORE the insert (module
    docstring, finding A2) -- a cross-org or nonexistent id raises the
    identical `TicketNotFoundError` either resolver already raises for every
    other cross-ref in this module, rather than reaching Postgres as a bare
    FK value that happens to exist in some other org's data.

    `sort_order` is an INSERT-time value, not a follow-up `update_category`
    call, for the identical reason `create_queue` takes it directly (module
    docstring, minor finding): one create request writes exactly one audit
    event, never a `ticket.category_created` immediately followed by a
    spurious `ticket.category_updated`.

    A duplicate `key` within the org is rejected up front as a typed
    `TicketValidationError` rather than surfacing the
    `uq_ticket_category_org_key` violation as a 500; the same `key` in a
    different org is unaffected.

    The pre-check above is a fast path, not the sole guard: two concurrent
    `create_category` calls for the same `(org_id, key)` can both miss it
    before either has written. The actual insert goes through `ON CONFLICT
    DO NOTHING` against `uq_ticket_category_org_key` -- but this is a
    single-caller CREATE, not a get-or-create: a race loser must get the
    identical typed `TicketValidationError` the pre-check raises, never
    someone else's row."""
    _reject_incident_requires_approval(type, requires_approval)

    if approver_role_id is not None:
        approver_role_id = (await resolve_role(session, org_id, approver_role_id)).id
    if default_queue_id is not None:
        default_queue_id = (await resolve_queue(session, org_id, default_queue_id)).id

    existing = (
        await session.execute(
            select(TicketCategory.id).where(
                TicketCategory.org_id == org_id, TicketCategory.key == key
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise TicketValidationError(tr("category key already exists: {key}", key=key))

    category_id = uuid7()
    inserted_id = (
        await session.execute(
            pg_insert(TicketCategory)
            .values(
                id=category_id,
                org_id=org_id,
                key=key,
                name=name,
                type=type,
                requires_approval=requires_approval,
                approver_role_id=approver_role_id,
                default_queue_id=default_queue_id,
                sort_order=sort_order,
                created_by=actor_id,
            )
            .on_conflict_do_nothing(index_elements=["org_id", "key"])
            .returning(TicketCategory.id)
        )
    ).scalar_one_or_none()
    if inserted_id is None:
        raise TicketValidationError(tr("category key already exists: {key}", key=key))

    category = (
        await session.execute(
            select(TicketCategory).where(
                TicketCategory.org_id == org_id, TicketCategory.id == inserted_id
            )
        )
    ).scalar_one()
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="ticket.category_created",
        target_type="ticket_category",
        target_id=str(category.id),
        metadata={"key": key, "name": name, "type": type, "requires_approval": requires_approval},
    )
    await session.flush()
    return category


async def resolve_category(
    session: AsyncSession, org_id: UUID, category_id: UUID
) -> TicketCategory:
    """Org-scoped load. A category id from another org is indistinguishable
    from a nonexistent one -- both raise `TicketNotFoundError` (spec §8,
    invariant #5)."""
    category = (
        await session.execute(
            select(TicketCategory).where(
                TicketCategory.org_id == org_id, TicketCategory.id == category_id
            )
        )
    ).scalar_one_or_none()
    if category is None:
        raise TicketNotFoundError(
            tr(
                "ticket category {category_id} not found",
                category_id=category_id,
            )
        )
    return category


async def update_category(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID,
    category_id: UUID,
    fields: dict[str, Any],
) -> TicketCategory:
    """Apply the SUPPLIED fields to a category, org-scoped-loaded. `key` and
    `type` are never touched here even if present in `fields` (both are
    absent from `_CATEGORY_EDITABLE_FIELDS`) -- changing `type` after
    creation could silently strand a `requires_approval=True` request
    category as an incident, exactly what the create-time guard forbids.

    A supplied `requires_approval=True` is re-checked against the category's
    (immutable) `type` here too, so a category already `type="incident"`
    cannot be flipped to `requires_approval=True` through an update either.

    A supplied `approver_role_id`/`default_queue_id` is resolved org-scoped
    via `resolve_role`/`resolve_queue` before `setattr`, the same as
    `create_category` (module docstring, finding A2) -- a cross-org or
    nonexistent id raises `TicketNotFoundError` rather than reaching Postgres
    unresolved. An explicit `null` for either (clearing it) is left alone:
    both are nullable, so there is nothing to resolve when the caller means
    "no queue"/"no approver role"."""
    _reject_null_for_non_nullable(fields, _CATEGORY_NON_NULLABLE_FIELDS)
    category = await resolve_category(session, org_id, category_id)

    if fields.get("requires_approval"):
        _reject_incident_requires_approval(category.type, True)

    if fields.get("approver_role_id") is not None:
        fields["approver_role_id"] = (
            await resolve_role(session, org_id, fields["approver_role_id"])
        ).id
    if fields.get("default_queue_id") is not None:
        fields["default_queue_id"] = (
            await resolve_queue(session, org_id, fields["default_queue_id"])
        ).id

    changed_fields = []
    for field in _CATEGORY_EDITABLE_FIELDS:
        if field not in fields:
            continue
        if getattr(category, field) != fields[field]:
            changed_fields.append(field)
        setattr(category, field, fields[field])
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="ticket.category_updated",
        target_type="ticket_category",
        target_id=str(category.id),
        metadata={"changed_fields": sorted(changed_fields)},
    )
    await session.flush()
    return category
