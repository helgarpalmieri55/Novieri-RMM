"""Reference data: expense categories and cost centers
(docs/modules/budget/spec.md §2, §8.1).

Mirrors `app.modules.subscriptions.service`'s org-scoped-load + audit + flush
style: every query here carries an explicit `org_id` predicate (invariant
#5), and every mutation calls `audit_service.record` and then flushes
(invariant #4).

`system_key` is the STABLE identifier the Task-10 auto-feeds resolve
against (spec §6.1): seeded rows get `system_key` set equal to their
`key` at creation and it is never touched again -- `update_category`'s
editable-field set deliberately excludes both `key` and `system_key`, so
renaming a category's display `name` can never change or clear it.
Operator-created categories get `system_key = None` -- they are not
resolvable by the auto-feeds, only by id/`key`.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from uuid_extensions import uuid7

from app.core.audit import audit_service
from app.core.i18n import tr
from app.models import CostCenter, ExpenseCategory
from app.modules.budget.errors import BudgetNotFoundError, BudgetValidationError

DEFAULT_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("hardware", "Hardware"),
    ("software_licenses", "Software licenses"),
    ("saas_subscriptions", "SaaS subscriptions"),
    ("cloud_hosting", "Cloud & hosting"),
    ("telecom", "Telecom"),
    ("services", "Services"),
    ("maintenance", "Maintenance"),
)

_DEFAULT_KEYS = tuple(key for key, _name in DEFAULT_CATEGORIES)

# Fields `update_category`/`update_cost_center` apply as plain scalar
# assignment, decided by key PRESENCE in `fields` (not by the value being
# non-None) -- callers pass `model_dump(exclude_unset=True)`, so a key is
# present only if actually supplied. `key`/`system_key` are deliberately
# absent from the category set: the stable identifier the auto-feeds
# resolve against must never move under a rename.
_CATEGORY_EDITABLE_FIELDS = ("name", "active", "sort_order")
_CATEGORY_NON_NULLABLE_FIELDS = frozenset({"name", "active", "sort_order"})

_COST_CENTER_EDITABLE_FIELDS = ("name", "external_ref", "active")
_COST_CENTER_NON_NULLABLE_FIELDS = frozenset({"name", "active"})


def _reject_null_for_non_nullable(
    fields: dict[str, Any], non_nullable: frozenset[str]
) -> None:
    """A supplied `None` for a NOT NULL column would otherwise reach Postgres
    as a NOT NULL violation and surface as a 500; reject it up front as a
    typed 400 instead (the `subscriptions.update_subscription` pattern)."""
    for field in non_nullable:
        if field in fields and fields[field] is None:
            raise BudgetValidationError(tr("{field} cannot be null", field=field))


# ---------------------------------------------------------------------------
# expense_category
# ---------------------------------------------------------------------------


async def seed_default_categories(
    session: AsyncSession, org_id: UUID, actor_id: UUID
) -> list[ExpenseCategory]:
    """Ensure the org has all of `DEFAULT_CATEGORIES`, inserting only the
    ones it is missing. Idempotent BY CONSTRUCTION -- the org's existing
    categories are selected first, so a second call for the same org
    inserts nothing, rather than being idempotent only because nothing
    happens to call it twice.

    This is a LAZY seed, called from every path that needs categories to
    exist (`list_categories`, `feeds.materialize`, `reporting.variance`,
    `reporting.forecast`) rather than from a one-time org-bootstrap hook --
    there is no such hook anywhere in the platform (C1 fix: previously this
    function was called by NOTHING in production, so `materialize`/
    `variance`/`forecast` 400'd for every real org, and no route could
    create a system-keyed category to recover). Being called on every
    request to those paths is exactly why the idempotent-by-construction
    property above, and the concurrency-safety below, both matter.

    The existence check covers `key`, not just `system_key`, because
    `uq_expense_category_org_key` is on `key`. An operator who created a
    category named e.g. "hardware" by hand (leaving `system_key` NULL)
    before this ever ran would otherwise collide with the seed and surface
    an IntegrityError as a 500 -- exactly the shape spec §8 forbids.

    Such a hand-made category is SKIPPED, not adopted: it keeps its NULL
    `system_key`, so the org simply has no category for that system key and
    Task 10's materialization fails loudly and namably (spec §6.1) instead
    of silently attributing subscription spend to a category an operator
    created for their own purpose. Adopting it would be a silent decision
    about how someone else's money is categorized.

    Concurrency-safe, mirroring `fx.get_settings` (Task-5 finding F2) and
    `feeds.materialize`'s own conflict-safe write (K3 fix): the in-memory
    `existing_keys` pre-check above is a fast path, not the sole guard --
    two concurrent first-ever calls for the same org (e.g. two requests
    racing `list_categories` on a brand-new org) could both miss it before
    either has written. The insert is a single batched
    `pg_insert(...).on_conflict_do_nothing()` against `uq_expense_category_org_key`
    rather than per-row `session.add` calls (which would let the loser of
    that race surface a raw `IntegrityError` as a 500 on a financial path,
    exactly what spec §8 forbids); `RETURNING` reports which rows actually
    landed, so the loser of a race is silently skipped rather than
    double-inserted or errored.

    Audit attribution (N1 fix): this function is reachable from every
    `budget.read` path (`list_categories`, `feeds.materialize`,
    `reporting.variance`, `reporting.forecast`) -- including a caller who
    holds ONLY `budget.read` (spec §8's `auditor` grant). Emitting the
    operator-facing `budget.category_created` event here, as this code used
    to, would attribute a bootstrap the reader never asked for to that
    reader in the immutable hash-chained log, as if they had managed the
    org's category taxonomy (`budget.manage`'s job, spec §8's RBAC split).
    So a seed that actually inserts something writes exactly ONE
    system-attributed event, `budget.categories_seeded` (`actor_type="system"`,
    `actor_id=None`, mirroring `security_logs.retention_applied`'s
    if-anything-happened style), naming the keys it inserted; a call that
    inserts nothing (the common case after the first) writes nothing at
    all. `created_by` on the row itself is unaffected -- it still records
    the acting user who tripped the bootstrap; only the AUDIT event's actor
    changes."""
    existing_keys = set(
        (
            await session.execute(
                select(ExpenseCategory.key).where(
                    ExpenseCategory.org_id == org_id,
                    ExpenseCategory.key.in_(_DEFAULT_KEYS),
                )
            )
        )
        .scalars()
        .all()
    )

    missing = [(key, name) for key, name in DEFAULT_CATEGORIES if key not in existing_keys]
    if missing:
        rows = [
            {
                "id": uuid7(),
                "org_id": org_id,
                "key": key,
                "name": name,
                "system_key": key,
                "created_by": actor_id,
            }
            for key, name in missing
        ]
        inserted = (
            await session.execute(
                pg_insert(ExpenseCategory)
                .values(rows)
                .on_conflict_do_nothing(index_elements=["org_id", "key"])
                .returning(ExpenseCategory.id, ExpenseCategory.key, ExpenseCategory.name)
            )
        ).all()
        await session.flush()

        if inserted:
            await audit_service.record(
                session,
                org_id=org_id,
                actor_type="system",
                actor_id=None,
                action="budget.categories_seeded",
                target_type="org",
                target_id=str(org_id),
                metadata={"keys": [key for _cat_id, key, _name in inserted]},
            )
            await session.flush()

    return list(
        (
            await session.execute(
                select(ExpenseCategory)
                .where(
                    ExpenseCategory.org_id == org_id,
                    ExpenseCategory.system_key.in_(_DEFAULT_KEYS),
                )
                .order_by(ExpenseCategory.sort_order, ExpenseCategory.key)
            )
        )
        .scalars()
        .all()
    )


async def list_categories(
    session: AsyncSession, org_id: UUID, actor_id: UUID, *, include_inactive: bool = False
) -> list[ExpenseCategory]:
    """Lazily seeds the org's default categories first (C1 fix) -- this is
    the first place a brand-new org's operator lands (`GET /budget/categories`
    populates the category picker every other budget route depends on), so
    it seeds from `actor_id` exactly like `feeds.materialize`/
    `reporting.variance`/`reporting.forecast` do."""
    await seed_default_categories(session, org_id, actor_id)
    stmt = select(ExpenseCategory).where(ExpenseCategory.org_id == org_id)
    if not include_inactive:
        stmt = stmt.where(ExpenseCategory.active.is_(True))
    stmt = stmt.order_by(ExpenseCategory.sort_order, ExpenseCategory.name)
    return list((await session.execute(stmt)).scalars().all())


async def create_category(
    session: AsyncSession, org_id: UUID, actor_id: UUID, *, key: str, name: str
) -> ExpenseCategory:
    """Create an operator category. `system_key` is always `None` here --
    only `seed_default_categories` ever sets it. A duplicate `key` within
    the org is rejected up front as a typed `BudgetValidationError` rather
    than surfacing the `uq_expense_category_org_key` violation as a 500;
    the same `key` in a different org is unaffected (the constraint, and
    this check, are both org-scoped).

    The pre-check above is a fast path, not the sole guard: two concurrent
    `create_category` calls for the same `(org_id, key)` can both miss it
    before either has written. A plain `session.add` here would let the
    loser surface a raw `IntegrityError` as a 500, so the actual insert
    instead goes through `ON CONFLICT DO NOTHING` against
    `uq_expense_category_org_key` (mirrors `seed_default_categories`'s own
    conflict-safe write just above) -- but this is a single-caller CREATE,
    not a get-or-create: a race loser must get the identical typed
    `BudgetValidationError` the pre-check raises, never someone else's
    row."""
    existing = (
        await session.execute(
            select(ExpenseCategory.id).where(
                ExpenseCategory.org_id == org_id, ExpenseCategory.key == key
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise BudgetValidationError(tr("category key already exists: {key}", key=key))

    cat_id = uuid7()
    inserted_id = (
        await session.execute(
            pg_insert(ExpenseCategory)
            .values(
                id=cat_id,
                org_id=org_id,
                key=key,
                name=name,
                system_key=None,
                created_by=actor_id,
            )
            .on_conflict_do_nothing(index_elements=["org_id", "key"])
            .returning(ExpenseCategory.id)
        )
    ).scalar_one_or_none()
    if inserted_id is None:
        raise BudgetValidationError(tr("category key already exists: {key}", key=key))

    cat = (
        await session.execute(select(ExpenseCategory).where(ExpenseCategory.id == inserted_id))
    ).scalar_one()
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="budget.category_created",
        target_type="expense_category",
        target_id=str(cat.id),
        metadata={"key": key, "name": name, "system_key": None},
    )
    await session.flush()
    return cat


async def resolve_category(
    session: AsyncSession, org_id: UUID, category_id: UUID
) -> ExpenseCategory:
    """Org-scoped load. A category id from another org is indistinguishable
    from a nonexistent one -- both raise `BudgetNotFoundError` (spec §8,
    invariant #5)."""
    cat = (
        await session.execute(
            select(ExpenseCategory).where(
                ExpenseCategory.org_id == org_id, ExpenseCategory.id == category_id
            )
        )
    ).scalar_one_or_none()
    if cat is None:
        raise BudgetNotFoundError(
            tr("expense category {category_id} not found", category_id=category_id)
        )
    return cat


async def category_by_system_key(
    session: AsyncSession, org_id: UUID, system_key: str
) -> ExpenseCategory | None:
    """Resolve the org's ACTIVE category for a `system_key` (spec §6.1: "no
    ACTIVE category with that system_key"). A category the operator has
    retired (`active=False`) is deliberately treated the same as one that
    was never seeded -- `None` -- so a caller like Task 10's `materialize`
    fails loudly and namably instead of silently booking spend against a
    category nobody reviews anymore.

    Contract change (K2 fix): this used to ignore `active` entirely. The
    only production caller is `feeds.materialize`, which already wants
    exactly this behavior (its error message has always said "no active
    expense category"); `list_categories`/`resolve_category`/`update_category`
    are unaffected, and no other module calls this function."""
    return (
        await session.execute(
            select(ExpenseCategory).where(
                ExpenseCategory.org_id == org_id,
                ExpenseCategory.system_key == system_key,
                ExpenseCategory.active.is_(True),
            )
        )
    ).scalar_one_or_none()


async def update_category(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID,
    category_id: UUID,
    fields: dict[str, Any],
) -> ExpenseCategory:
    """Apply the SUPPLIED fields to a category, org-scoped-loaded.
    `key`/`system_key` are never touched here even if present in `fields`
    (they are absent from `_CATEGORY_EDITABLE_FIELDS`) -- `system_key` is
    the stable identifier the Task-10 auto-feeds resolve against, and
    renaming the display `name` must never change or clear it."""
    _reject_null_for_non_nullable(fields, _CATEGORY_NON_NULLABLE_FIELDS)
    cat = await resolve_category(session, org_id, category_id)

    changed_fields = []
    for field in _CATEGORY_EDITABLE_FIELDS:
        if field not in fields:
            continue
        if getattr(cat, field) != fields[field]:
            changed_fields.append(field)
        setattr(cat, field, fields[field])
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="budget.category_updated",
        target_type="expense_category",
        target_id=str(cat.id),
        metadata={"changed_fields": sorted(changed_fields)},
    )
    await session.flush()
    return cat


# ---------------------------------------------------------------------------
# cost_center
# ---------------------------------------------------------------------------


async def create_cost_center(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID,
    *,
    code: str,
    name: str,
    external_ref: str | None = None,
) -> CostCenter:
    """A duplicate `code` within the org is rejected up front as a typed
    `BudgetValidationError` rather than surfacing the
    `uq_cost_center_org_code` violation as a 500; the same `code` in a
    different org is unaffected.

    The pre-check above is a fast path, not the sole guard: two concurrent
    `create_cost_center` calls for the same `(org_id, code)` can both miss
    it before either has written. A plain `session.add` here would let the
    loser surface a raw `IntegrityError` as a 500, so the actual insert
    instead goes through `ON CONFLICT DO NOTHING` against
    `uq_cost_center_org_code` (same shape as `create_category` above) --
    the race loser gets the identical typed `BudgetValidationError` the
    pre-check raises, never someone else's row."""
    existing = (
        await session.execute(
            select(CostCenter.id).where(CostCenter.org_id == org_id, CostCenter.code == code)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise BudgetValidationError(tr("cost center code already exists: {code}", code=code))

    cc_id = uuid7()
    inserted_id = (
        await session.execute(
            pg_insert(CostCenter)
            .values(
                id=cc_id,
                org_id=org_id,
                code=code,
                name=name,
                external_ref=external_ref,
                created_by=actor_id,
            )
            .on_conflict_do_nothing(index_elements=["org_id", "code"])
            .returning(CostCenter.id)
        )
    ).scalar_one_or_none()
    if inserted_id is None:
        raise BudgetValidationError(tr("cost center code already exists: {code}", code=code))

    cc = (
        await session.execute(select(CostCenter).where(CostCenter.id == inserted_id))
    ).scalar_one()
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="budget.cost_center_created",
        target_type="cost_center",
        target_id=str(cc.id),
        metadata={"code": code, "name": name},
    )
    await session.flush()
    return cc


async def list_cost_centers(
    session: AsyncSession, org_id: UUID, *, include_inactive: bool = False
) -> list[CostCenter]:
    stmt = select(CostCenter).where(CostCenter.org_id == org_id)
    if not include_inactive:
        stmt = stmt.where(CostCenter.active.is_(True))
    stmt = stmt.order_by(CostCenter.code)
    return list((await session.execute(stmt)).scalars().all())


async def resolve_cost_center(
    session: AsyncSession, org_id: UUID, cost_center_id: UUID
) -> CostCenter:
    """Org-scoped load. A cost center id from another org is
    indistinguishable from a nonexistent one -- both raise
    `BudgetNotFoundError` (spec §8, invariant #5)."""
    cc = (
        await session.execute(
            select(CostCenter).where(
                CostCenter.org_id == org_id, CostCenter.id == cost_center_id
            )
        )
    ).scalar_one_or_none()
    if cc is None:
        raise BudgetNotFoundError(
            tr("cost center {cost_center_id} not found", cost_center_id=cost_center_id)
        )
    return cc


async def update_cost_center(
    session: AsyncSession,
    org_id: UUID,
    actor_id: UUID,
    cost_center_id: UUID,
    fields: dict[str, Any],
) -> CostCenter:
    """Apply the SUPPLIED fields to a cost center, org-scoped-loaded. `code`
    is never touched here (absent from `_COST_CENTER_EDITABLE_FIELDS`)."""
    _reject_null_for_non_nullable(fields, _COST_CENTER_NON_NULLABLE_FIELDS)
    cc = await resolve_cost_center(session, org_id, cost_center_id)

    changed_fields = []
    for field in _COST_CENTER_EDITABLE_FIELDS:
        if field not in fields:
            continue
        if getattr(cc, field) != fields[field]:
            changed_fields.append(field)
        setattr(cc, field, fields[field])
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="budget.cost_center_updated",
        target_type="cost_center",
        target_id=str(cc.id),
        metadata={"changed_fields": sorted(changed_fields)},
    )
    await session.flush()
    return cc
