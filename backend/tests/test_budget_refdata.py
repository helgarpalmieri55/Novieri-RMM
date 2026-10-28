"""Reference data: expense categories and cost centers (spec §2, §8.1).

Covers: seeding is idempotent by construction (running twice does not
duplicate); `system_key` is set on seeded rows and `None` on
operator-created ones; renaming a category's display name leaves
`system_key` intact; a duplicate `key`/`code` within one org is rejected;
the same `key`/`code` in two orgs is fine; `resolve_category` and
`resolve_cost_center` raise `BudgetNotFoundError` for another org's id --
each with a load-bearing tenancy test on a raw BYPASSRLS admin session.
"""

from uuid import UUID

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import CostCenter, ExpenseCategory, User
from app.modules.budget.errors import BudgetNotFoundError, BudgetValidationError
from app.modules.budget.refdata import (
    DEFAULT_CATEGORIES,
    category_by_system_key,
    create_category,
    create_cost_center,
    list_categories,
    list_cost_centers,
    resolve_category,
    resolve_cost_center,
    seed_default_categories,
    update_category,
    update_cost_center,
)


async def _org_with_user(api_env) -> tuple[UUID, UUID]:
    """Create a throwaway org plus an `org-admin` user in it; return
    `(org_id, user_id)`. Mirrors test_budget_fx.py's helper of the same
    name/shape -- not imported across test modules, per convention."""
    org_id = await api_env.create_org("refdata")
    sub = await api_env.create_user(org_id, "org-admin")
    async with AsyncSession(api_env.admin_engine, expire_on_commit=False) as session:
        user_id = (
            await session.execute(select(User.id).where(User.external_subject == sub))
        ).scalar_one()
    return org_id, user_id


# ---------------------------------------------------------------------------
# seeding
# ---------------------------------------------------------------------------


async def test_seed_default_categories_creates_all_seven_with_matching_system_key(
    api_env,
) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cats = await seed_default_categories(session, org_id, user_id, client_id=org_id)
    assert {c.key for c in cats} == {key for key, _name in DEFAULT_CATEGORIES}
    assert {c.name for c in cats} == {name for _key, name in DEFAULT_CATEGORIES}
    assert all(c.system_key == c.key for c in cats)
    assert all(c.org_id == org_id for c in cats)


async def test_seed_default_categories_is_idempotent_by_construction(api_env) -> None:
    """Running it twice must not duplicate. This is the property, not a
    lucky consequence of nothing calling it twice: `seed_default_categories`
    selects the org's existing `system_key`s first and inserts only the
    ones missing."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        first = await seed_default_categories(session, org_id, user_id, client_id=org_id)
        second = await seed_default_categories(session, org_id, user_id, client_id=org_id)
    assert len(first) == 7
    assert len(second) == 7
    assert {c.id for c in first} == {c.id for c in second}

    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        count = (
            await session.execute(
                select(func.count())
                .select_from(ExpenseCategory)
                .where(ExpenseCategory.org_id == org_id)
            )
        ).scalar_one()
    assert count == 7


async def test_seed_emits_one_system_event_not_per_row_user_events(api_env) -> None:
    """N1 fix: the seed is a system bootstrap, not an operator action, on
    EVERY path that reaches it (`list_categories`/`feeds.materialize`/
    `reporting.variance`/`reporting.forecast` all funnel through this one
    function). The first call for a fresh org must write ZERO
    `budget.category_created` events (that event is reserved for
    `create_category`, the deliberate operator action) and exactly ONE
    `budget.categories_seeded`, system-attributed and naming the seven keys
    it inserted. A second call, which inserts nothing, must write nothing at
    all -- not even a second `budget.categories_seeded`."""
    from app.models import AuditEvent

    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await seed_default_categories(session, org_id, user_id, client_id=org_id)

        created_events = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.action == "budget.category_created",
                )
            )
        ).scalars().all()
        seeded_events = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.action == "budget.categories_seeded",
                )
            )
        ).scalars().all()

    assert created_events == []
    assert len(seeded_events) == 1
    event = seeded_events[0]
    assert event.actor_type == "system"
    assert event.actor_id is None
    assert set(event.meta["keys"]) == {key for key, _name in DEFAULT_CATEGORIES}

    # A second call inserts nothing, so it must audit nothing -- not even a
    # second `budget.categories_seeded`.
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await seed_default_categories(session, org_id, user_id, client_id=org_id)
        seeded_events_after = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.action == "budget.categories_seeded",
                )
            )
        ).scalars().all()
    assert len(seeded_events_after) == 1


async def test_seed_skips_a_hand_made_category_whose_key_collides(api_env) -> None:
    """An operator can create a category named "hardware" (system_key NULL)
    before the seed ever runs. `uq_expense_category_org_key` is on `key`, so
    a seed that checked only `system_key` would try to insert a second
    `key="hardware"` row and raise IntegrityError -- a 500, which spec §8
    forbids for a validation-shaped failure.

    The colliding default is skipped, not adopted: the operator's row keeps
    its NULL system_key, so `category_by_system_key(..., "hardware")` finds
    nothing and Task 10's materialization fails loudly per spec §6.1 rather
    than silently booking spend against a category made for another purpose.
    """
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        hand_made = await create_category(
            session, org_id, user_id, client_id=org_id, key="hardware", name="Hardware (ours)"
        )
        assert hand_made.system_key is None

        seeded = await seed_default_categories(session, org_id, user_id, client_id=org_id)

        count = (
            await session.execute(
                select(func.count())
                .select_from(ExpenseCategory)
                .where(ExpenseCategory.org_id == org_id)
            )
        ).scalar_one()
        adopted = await category_by_system_key(session, org_id, "hardware", client_id=org_id)

    # six seeded defaults + the operator's own row; "hardware" was not re-inserted
    assert count == 7
    # only SIX system categories exist -- the org genuinely has no
    # system_key="hardware", which is the loud-failure state spec §6.1 wants
    assert len(seeded) == 6
    assert {c.system_key for c in seeded} == {k for k, _ in DEFAULT_CATEGORIES} - {"hardware"}
    # the operator's row is untouched and did NOT silently become the system one
    assert adopted is None
    assert hand_made.name == "Hardware (ours)"
    assert hand_made.system_key is None


async def test_seed_default_categories_backfills_only_the_missing_ones(api_env) -> None:
    """If an org already has some (but not all) default categories -- e.g.
    from a partial prior seed -- a later call must add only what is
    missing, not re-touch what already exists."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        pre_existing = ExpenseCategory(
            org_id=org_id, client_id=org_id,
            key="hardware",
            name="Custom Hardware Name",
            system_key="hardware",
            created_by=user_id,
        )
        session.add(pre_existing)
        await session.flush()
        pre_existing_id = pre_existing.id

        cats = await seed_default_categories(session, org_id, user_id, client_id=org_id)

    assert len(cats) == 7
    by_key = {c.key: c for c in cats}
    # The pre-existing row was left alone -- same id, same (untouched) name.
    assert by_key["hardware"].id == pre_existing_id
    assert by_key["hardware"].name == "Custom Hardware Name"
    # The other six were actually created.
    assert by_key["telecom"].system_key == "telecom"


async def test_operator_created_category_has_no_system_key(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cat = await create_category(session, org_id, user_id, client_id=org_id,
                                    key="training", name="Training")
    assert cat.key == "training"
    assert cat.name == "Training"
    assert cat.system_key is None


async def test_renaming_a_category_preserves_its_system_key(api_env) -> None:
    """system_key is the stable identifier the auto-feeds resolve against
    (spec §6.1) -- renaming the display name must never break materialization."""
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await seed_default_categories(session, org_id, user_id, client_id=org_id)
        cat = await category_by_system_key(session, org_id, "saas_subscriptions", client_id=org_id)
        assert cat is not None
        await update_category(session, org_id, user_id, cat.id, {"name": "Cloud SaaS"})
        again = await category_by_system_key(
            session, org_id, "saas_subscriptions", client_id=org_id
        )
    assert again is not None
    assert again.name == "Cloud SaaS"
    assert again.system_key == "saas_subscriptions"


async def test_category_by_system_key_returns_none_when_absent(api_env) -> None:
    org_id, _user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cat = await category_by_system_key(session, org_id, "does_not_exist", client_id=org_id)
    assert cat is None


async def test_duplicate_key_in_one_org_is_rejected(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await create_category(session, org_id, user_id, client_id=org_id,
                              key="widgets", name="Widgets")
        with pytest.raises(BudgetValidationError):
            await create_category(session, org_id, user_id, client_id=org_id,
                                  key="widgets", name="Widgets 2")


async def test_create_category_concurrent_duplicate_raises_typed_error_not_integrity_error(
    api_env, admin_engine: AsyncEngine
) -> None:
    """Two concurrent `create_category` calls for the same `(org_id, key)`
    can both miss the in-memory pre-check before either has written. The
    fix mirrors `seed_default_categories`'s own conflict-safe write: the
    actual insert goes through `ON CONFLICT DO NOTHING` against
    `uq_expense_category_org_key` -- but this is a single-caller CREATE,
    not a get-or-create, so the race loser must raise the SAME typed
    `BudgetValidationError` the pre-check raises, not silently return
    someone else's category or 500 on a raw `IntegrityError`.

    Deterministic interleave, same technique as
    test_get_settings_concurrent_first_calls_do_not_race in
    test_budget_fx.py: session A's pre-check SELECT against
    `expense_category` is intercepted, and right after it returns (a miss
    -- nothing exists yet), session B inserts and commits the EXACT row
    session A is about to try to create."""
    org_id, user_id = await _org_with_user(api_env)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session_a:
        original_execute = session_a.execute
        precheck_seen = False

        async def interleaving_execute(statement, *args, **kwargs):
            nonlocal precheck_seen
            result = await original_execute(statement, *args, **kwargs)
            if (
                not precheck_seen
                and getattr(statement, "is_select", False)
                and "expense_category.id" in str(statement).lower()
            ):
                precheck_seen = True
                async with AsyncSession(admin_engine, expire_on_commit=False) as session_b:
                    winner = ExpenseCategory(
                        org_id=org_id, client_id=org_id,
                        key="widgets",
                        name="Widgets (winner)",
                        system_key=None,
                        created_by=user_id,
                    )
                    session_b.add(winner)
                    await session_b.commit()
            return result

        session_a.execute = interleaving_execute  # type: ignore[method-assign]
        with pytest.raises(BudgetValidationError, match="category key already exists"):
            await create_category(session_a, org_id, user_id, client_id=org_id,
                                  key="widgets", name="Widgets")
        await session_a.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        rows = (
            (
                await session.execute(
                    select(ExpenseCategory).where(
                        ExpenseCategory.org_id == org_id, ExpenseCategory.key == "widgets"
                    )
                )
            )
            .scalars()
            .all()
        )
    # Exactly one row exists -- session B's, not a duplicate.
    assert len(rows) == 1
    assert rows[0].name == "Widgets (winner)"


async def test_same_key_in_two_orgs_is_fine(api_env) -> None:
    org_a, user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)
    async with org_session(org_a, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cat_a = await create_category(session, org_a, user_a, client_id=org_a,
                                      key="widgets", name="Widgets")
    async with org_session(org_b, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cat_b = await create_category(session, org_b, user_b, client_id=org_b,
                                      key="widgets", name="Widgets")
    assert cat_a.key == cat_b.key == "widgets"
    assert cat_a.id != cat_b.id
    assert cat_a.org_id != cat_b.org_id


async def test_list_categories_excludes_inactive_by_default(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cat = await create_category(session, org_id, user_id, client_id=org_id,
                                    key="widgets", name="Widgets")
        await update_category(session, org_id, user_id, cat.id, {"active": False})
        active_only = await list_categories(session, org_id, user_id)
        with_inactive = await list_categories(session, org_id, user_id, include_inactive=True)
    assert cat.id not in {c.id for c in active_only}
    assert cat.id in {c.id for c in with_inactive}


async def test_update_category_rejects_null_name(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cat = await create_category(session, org_id, user_id, client_id=org_id,
                                    key="widgets", name="Widgets")
        with pytest.raises(BudgetValidationError):
            await update_category(session, org_id, user_id, cat.id, {"name": None})


async def test_update_category_reports_only_the_changed_field_names(api_env) -> None:
    """Mirrors the Phase-12 edit-audit convention: metadata carries changed
    field NAMES only. Passing an unchanged value alongside a real change
    must not report the unchanged one as changed."""
    from app.models import AuditEvent

    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cat = await create_category(session, org_id, user_id, client_id=org_id,
                                    key="widgets", name="Widgets")
        await update_category(
            session, org_id, user_id, cat.id, {"name": "Widgets", "sort_order": 5}
        )
        event = (
            await session.execute(
                select(AuditEvent)
                .where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.action == "budget.category_updated",
                )
                .order_by(AuditEvent.id.desc())
                .limit(1)
            )
        ).scalar_one()
    assert event.meta["changed_fields"] == ["sort_order"]


# ---------------------------------------------------------------------------
# cost_center
# ---------------------------------------------------------------------------


async def test_create_cost_center_and_resolve(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cc = await create_cost_center(session, org_id, user_id, client_id=org_id,
                                      code="ENG", name="Engineering")
        resolved = await resolve_cost_center(session, org_id, cc.id)
    assert resolved.id == cc.id
    assert resolved.code == "ENG"
    assert resolved.name == "Engineering"
    assert resolved.external_ref is None
    assert resolved.active is True


async def test_duplicate_cost_center_code_in_one_org_is_rejected(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        await create_cost_center(session, org_id, user_id, client_id=org_id,
                                 code="ENG", name="Engineering")
        with pytest.raises(BudgetValidationError):
            await create_cost_center(session, org_id, user_id, client_id=org_id,
                                     code="ENG", name="Eng 2")


async def test_create_cost_center_concurrent_duplicate_raises_typed_error_not_integrity_error(
    api_env, admin_engine: AsyncEngine
) -> None:
    """Same shape as `create_category`'s equivalent test: two concurrent
    `create_cost_center` calls for the same `(org_id, code)` can both miss
    the pre-check before either has written. The race loser must raise the
    SAME typed `BudgetValidationError` the pre-check raises, not a raw
    `IntegrityError`.

    Deterministic interleave: session A's pre-check SELECT against
    `cost_center` is intercepted, and right after it returns (a miss),
    session B inserts and commits the EXACT row session A is about to try
    to create."""
    org_id, user_id = await _org_with_user(api_env)

    async with AsyncSession(admin_engine, expire_on_commit=False) as session_a:
        original_execute = session_a.execute
        precheck_seen = False

        async def interleaving_execute(statement, *args, **kwargs):
            nonlocal precheck_seen
            result = await original_execute(statement, *args, **kwargs)
            if (
                not precheck_seen
                and getattr(statement, "is_select", False)
                and "cost_center.id" in str(statement).lower()
            ):
                precheck_seen = True
                async with AsyncSession(admin_engine, expire_on_commit=False) as session_b:
                    winner = CostCenter(
                        org_id=org_id, client_id=org_id,
                        code="ENG",
                        name="Engineering (winner)",
                        created_by=user_id,
                    )
                    session_b.add(winner)
                    await session_b.commit()
            return result

        session_a.execute = interleaving_execute  # type: ignore[method-assign]
        with pytest.raises(BudgetValidationError, match="cost center code already exists"):
            await create_cost_center(session_a, org_id, user_id, client_id=org_id,
                                     code="ENG", name="Engineering")
        await session_a.commit()

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        rows = (
            (
                await session.execute(
                    select(CostCenter).where(
                        CostCenter.org_id == org_id, CostCenter.code == "ENG"
                    )
                )
            )
            .scalars()
            .all()
        )
    # Exactly one row exists -- session B's, not a duplicate.
    assert len(rows) == 1
    assert rows[0].name == "Engineering (winner)"


async def test_same_cost_center_code_in_two_orgs_is_fine(api_env) -> None:
    org_a, user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)
    async with org_session(org_a, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cc_a = await create_cost_center(session, org_a, user_a, client_id=org_a,
                                        code="ENG", name="Engineering")
    async with org_session(org_b, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cc_b = await create_cost_center(session, org_b, user_b, client_id=org_b,
                                        code="ENG", name="Engineering")
    assert cc_a.code == cc_b.code == "ENG"
    assert cc_a.id != cc_b.id


async def test_update_cost_center_changes_name_and_external_ref(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cc = await create_cost_center(session, org_id, user_id, client_id=org_id,
                                      code="ENG", name="Engineering")
        updated = await update_cost_center(
            session, org_id, user_id, cc.id, {"name": "Eng Org", "external_ref": "odoo-42"}
        )
    assert updated.name == "Eng Org"
    assert updated.external_ref == "odoo-42"
    # code is not an editable field -- it must stay put.
    assert updated.code == "ENG"


async def test_update_cost_center_rejects_null_name(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cc = await create_cost_center(session, org_id, user_id, client_id=org_id,
                                      code="ENG", name="Engineering")
        with pytest.raises(BudgetValidationError):
            await update_cost_center(session, org_id, user_id, cc.id, {"name": None})


async def test_list_cost_centers_excludes_inactive_by_default(api_env) -> None:
    org_id, user_id = await _org_with_user(api_env)
    async with org_session(org_id, engine=api_env.app_engine, clients=ClientScope.all()) as session:
        cc = await create_cost_center(session, org_id, user_id, client_id=org_id,
                                      code="ENG", name="Engineering")
        await update_cost_center(session, org_id, user_id, cc.id, {"active": False})
        active_only = await list_cost_centers(session, org_id)
        with_inactive = await list_cost_centers(session, org_id, include_inactive=True)
    assert cc.id not in {c.id for c in active_only}
    assert cc.id in {c.id for c in with_inactive}


# ---------------------------------------------------------------------------
# tenancy -- load-bearing, on a raw BYPASSRLS admin session
# ---------------------------------------------------------------------------


async def test_tenancy_resolve_category_org_filter_is_load_bearing(
    api_env, admin_engine: AsyncEngine
) -> None:
    """Org A must never resolve org B's category by id.

    Runs on a raw AsyncSession(admin_engine) -- its_admin is BYPASSRLS
    (tests/conftest.py:60) -- so org B's row is fully visible on this
    connection and resolve_category's own org_id predicate is the SOLE
    guard. Under org_session(engine=app_engine) RLS would mask org B's row
    and this test would pass even with the predicate deleted.

    `id` is the table's primary key (globally unique, not just per-org), so
    dropping the `org_id` predicate does not create a `MultipleResultsFound`
    ambiguity here regardless of what other rows exist in the table --
    `select(ExpenseCategory).where(ExpenseCategory.id == cat_b_id)` matches
    exactly org B's row either way. That makes this a clean, deterministic
    assertion about WHICH org's category comes back, not an incidental
    row-count artifact.
    """
    org_a, _user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        cat_b = ExpenseCategory(
            org_id=org_b, client_id=org_b, key="widgets", name="Org B Widgets", created_by=user_b
        )
        session.add(cat_b)
        await session.commit()
        cat_b_id = cat_b.id

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(BudgetNotFoundError):
            await resolve_category(session, org_a, cat_b_id)


async def test_tenancy_resolve_cost_center_org_filter_is_load_bearing(
    api_env, admin_engine: AsyncEngine
) -> None:
    """Org A must never resolve org B's cost center by id. Same shape and
    same reasoning as the category tenancy test above -- `id` is the
    table's primary key, so this is deterministic regardless of what other
    rows are present."""
    org_a, _user_a = await _org_with_user(api_env)
    org_b, user_b = await _org_with_user(api_env)
    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        cc_b = CostCenter(
            org_id=org_b, client_id=org_b, code="ENG", name="Org B Engineering", created_by=user_b
        )
        session.add(cc_b)
        await session.commit()
        cc_b_id = cc_b.id

    async with AsyncSession(admin_engine, expire_on_commit=False) as session:
        with pytest.raises(BudgetNotFoundError):
            await resolve_cost_center(session, org_a, cc_b_id)
