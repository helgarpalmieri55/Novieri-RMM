"""Tests for the Licenses/SAM matching engine and product service
(docs/modules/licenses/spec.md §4): rule-based install matching that computes
the compliance position an audit depends on.

Mirrors `tests/test_licenses_vendor.py`'s fixture style (throwaway org/user
seeded via `admin_engine`, service calls through the RLS-constrained
`org_session`) and `tests/test_bcp_test_service.py`'s `_seed_device` helper
plus its load-bearing-tenancy pattern
(`test_evidence_source_queries_org_filter_is_load_bearing`): run the query
directly on an RLS-bypassing admin session so the explicit `org_id` predicate
is the sole guard, not RLS.
"""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import AuditEvent, Device, InstalledSoftware, Organization, User
from app.modules.licenses.errors import LicenseNotFoundError, LicenseValidationError
from app.modules.licenses.matching import (
    product_installed_device_ids,
    rule_matches,
    validate_match_rules,
)
from app.modules.licenses.service import (
    create_product,
    get_product,
    list_products,
    update_product,
)

# ---------------------------------------------------------------------------
# seeding helpers
# ---------------------------------------------------------------------------


async def _org_with_user(admin_engine: AsyncEngine) -> tuple[UUID, UUID]:
    slug = f"licmatch-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name=slug, slug=slug)
        s.add(org)
        await s.flush()
        user = User(org_id=org.id, external_subject=f"sub-{slug}", email=f"{slug}@example.com")
        s.add(user)
        await s.commit()
        return org.id, user.id


async def _seed_device(admin_engine: AsyncEngine, org_id: UUID, hostname: str) -> UUID:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        device = Device(org_id=org_id, hostname=hostname, os_family="linux")
        s.add(device)
        await s.commit()
        return device.id


async def _seed_installed(
    admin_engine: AsyncEngine,
    org_id: UUID,
    device_id: UUID,
    name: str,
    publisher: str = "",
) -> UUID:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        row = InstalledSoftware(
            org_id=org_id, device_id=device_id, name=name, publisher=publisher
        )
        s.add(row)
        await s.commit()
        return row.id


async def _audit_actions(
    app_engine: AsyncEngine, org_id: UUID, target_type: str, target_id: UUID
) -> list[str]:
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        events = (
            await s.execute(
                select(AuditEvent)
                .where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.target_type == target_type,
                    AuditEvent.target_id == str(target_id),
                )
                # Callers assert on list ORDER, so the query must define one.
                # `id` is uuid7 and therefore chain-monotonic.
                .order_by(AuditEvent.id)
            )
        ).scalars().all()
        return [e.action for e in events]


# ---------------------------------------------------------------------------
# rule_matches
# ---------------------------------------------------------------------------


def test_rule_matches_exact_case_insensitive() -> None:
    rule = {"type": "exact", "pattern": "Acme App", "publisher": None}
    assert rule_matches(rule, "acme app", "") is True
    assert rule_matches(rule, "Acme App Pro", "") is False


def test_rule_matches_prefix_case_insensitive() -> None:
    rule = {"type": "prefix", "pattern": "Acme", "publisher": None}
    assert rule_matches(rule, "Acme Pro", "") is True
    assert rule_matches(rule, "acme lite", "") is True
    assert rule_matches(rule, "xAcme", "") is False


def test_rule_matches_regex_is_anchored_fullmatch() -> None:
    rule = {"type": "regex", "pattern": "Acme.*", "publisher": None}
    assert rule_matches(rule, "Acme Pro", "") is True
    assert rule_matches(rule, "xAcme Pro", "") is False


def test_rule_matches_publisher_constraint_filters() -> None:
    rule = {"type": "exact", "pattern": "Acme App", "publisher": "Acme Inc"}
    assert rule_matches(rule, "Acme App", "Acme Inc") is True
    assert rule_matches(rule, "Acme App", "acme inc") is True  # case-insensitive
    assert rule_matches(rule, "Acme App", "Other Publisher") is False


def test_rule_matches_no_publisher_constraint_when_unset() -> None:
    rule = {"type": "exact", "pattern": "Acme App", "publisher": None}
    assert rule_matches(rule, "Acme App", "Anyone") is True


def test_rule_matches_regex_compile_error_returns_false_defensively() -> None:
    rule = {"type": "regex", "pattern": "[", "publisher": None}
    assert rule_matches(rule, "anything", "") is False


# ---------------------------------------------------------------------------
# validate_match_rules
# ---------------------------------------------------------------------------


def test_validate_match_rules_accepts_valid_set() -> None:
    validate_match_rules(
        [
            {"type": "exact", "pattern": "Acme App", "publisher": None},
            {"type": "prefix", "pattern": "Acme", "publisher": "Acme Inc"},
            {"type": "regex", "pattern": "Acme.*", "publisher": None},
        ]
    )


def test_validate_match_rules_rejects_unknown_type() -> None:
    with pytest.raises(LicenseValidationError):
        validate_match_rules([{"type": "fuzzy", "pattern": "Acme", "publisher": None}])


def test_validate_match_rules_rejects_empty_pattern() -> None:
    with pytest.raises(LicenseValidationError):
        validate_match_rules([{"type": "exact", "pattern": "", "publisher": None}])


def test_validate_match_rules_rejects_oversized_pattern() -> None:
    with pytest.raises(LicenseValidationError):
        validate_match_rules(
            [{"type": "exact", "pattern": "a" * 201, "publisher": None}]
        )


def test_validate_match_rules_rejects_uncompilable_regex() -> None:
    with pytest.raises(LicenseValidationError):
        validate_match_rules([{"type": "regex", "pattern": "[", "publisher": None}])


def test_validate_match_rules_rejects_non_list() -> None:
    with pytest.raises(LicenseValidationError):
        validate_match_rules({"type": "exact", "pattern": "Acme"})  # type: ignore[arg-type]


def test_validate_match_rules_rejects_missing_type() -> None:
    with pytest.raises(LicenseValidationError):
        validate_match_rules([{"pattern": "Acme", "publisher": None}])


# ---------------------------------------------------------------------------
# product_installed_device_ids
# ---------------------------------------------------------------------------


async def _seed_product(
    app_engine: AsyncEngine, org_id: UUID, actor_id: UUID, match_rules: list[dict]
):
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        product = await create_product(
            s,
            org_id=org_id,
            actor_id=actor_id,
            name="Acme Suite",
            publisher="Acme Inc",
            match_rules=match_rules,
        )
        await s.commit()
        return product


async def test_product_installed_device_ids_matches_own_org_only(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    other_org_id, _other_actor = await _org_with_user(admin_engine)

    device1 = await _seed_device(admin_engine, org_id, "device-1")
    device2 = await _seed_device(admin_engine, org_id, "device-2")
    device3 = await _seed_device(admin_engine, org_id, "device-3")
    foreign_device = await _seed_device(admin_engine, other_org_id, "foreign-device")

    match_rules = [
        {"type": "exact", "pattern": "Acme Pro", "publisher": None},
        {"type": "prefix", "pattern": "Acme Lite", "publisher": None},
    ]

    await _seed_installed(admin_engine, org_id, device1, "Acme Pro")
    # a second matching row on device1 -- still counted once (it's a set)
    await _seed_installed(admin_engine, org_id, device1, "Acme Lite Extra")
    await _seed_installed(admin_engine, org_id, device2, "Acme Lite Extra")
    await _seed_installed(admin_engine, org_id, device3, "Other App")
    await _seed_installed(admin_engine, other_org_id, foreign_device, "Acme Pro")

    product = await _seed_product(app_engine, org_id, actor_id, match_rules)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        device_ids = await product_installed_device_ids(s, org_id, product)

    assert device_ids == {device1, device2}


async def test_product_installed_device_ids_org_filter_is_load_bearing(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Run the query on an RLS-bypassing admin session (the `admin_engine`
    role bypasses RLS) with a foreign-org matching device present, so the
    explicit `InstalledSoftware.org_id == org_id` predicate in
    `product_installed_device_ids` is the SOLE guard against cross-tenant
    leakage -- RLS is not backstopping it here.
    """
    org_id, actor_id = await _org_with_user(admin_engine)
    other_org_id, _other_actor = await _org_with_user(admin_engine)

    device1 = await _seed_device(admin_engine, org_id, "device-1")
    foreign_device = await _seed_device(admin_engine, other_org_id, "foreign-device")

    match_rules = [{"type": "exact", "pattern": "Acme Pro", "publisher": None}]

    await _seed_installed(admin_engine, org_id, device1, "Acme Pro")
    await _seed_installed(admin_engine, other_org_id, foreign_device, "Acme Pro")

    product = await _seed_product(app_engine, org_id, actor_id, match_rules)

    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        device_ids = await product_installed_device_ids(s, org_id, product)

    assert device_ids == {device1}
    assert foreign_device not in device_ids


# ---------------------------------------------------------------------------
# create_product / update_product / get_product / list_products
# ---------------------------------------------------------------------------


async def test_create_product_succeeds_and_audits(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        product = await create_product(
            s,
            org_id=org_id,
            actor_id=actor_id,
            name="Acme Suite",
            publisher="Acme Inc",
            match_rules=[{"type": "exact", "pattern": "Acme Suite", "publisher": None}],
        )
        await s.commit()
        product_id = product.id

    assert product.name == "Acme Suite"
    assert product.created_by == actor_id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        fetched = await get_product(s, org_id, product_id)
        assert fetched is not None
        assert fetched.name == "Acme Suite"

        all_products = await list_products(s, org_id)
        assert [p.id for p in all_products] == [product_id]

    actions = await _audit_actions(app_engine, org_id, "product", product_id)
    assert actions == ["license.product_created"]

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        event = (
            await s.execute(
                select(AuditEvent).where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.action == "license.product_created",
                )
            )
        ).scalar_one()
        assert event.meta["name"] == "Acme Suite"


async def test_create_product_rejects_invalid_match_rules_before_persist(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(LicenseValidationError):
            await create_product(
                s,
                org_id=org_id,
                actor_id=actor_id,
                name="Bad Product",
                publisher="",
                match_rules=[{"type": "bogus", "pattern": "x", "publisher": None}],
            )

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        all_products = await list_products(s, org_id)
        assert all_products == []


async def test_update_product_applies_fields_and_audits(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        product = await create_product(
            s,
            org_id=org_id,
            actor_id=actor_id,
            name="Original",
            publisher="Orig Inc",
            match_rules=[{"type": "exact", "pattern": "Original", "publisher": None}],
        )
        await s.commit()
        product_id = product.id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        updated = await update_product(
            s,
            org_id=org_id,
            actor_id=actor_id,
            product_id=product_id,
            publisher="Updated Inc",
        )
        await s.commit()
        assert updated.publisher == "Updated Inc"
        assert updated.name == "Original"  # None field left unchanged

    actions = await _audit_actions(app_engine, org_id, "product", product_id)
    assert actions == ["license.product_created", "license.product_updated"]


async def test_update_product_bad_match_rules_raises(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        product = await create_product(
            s,
            org_id=org_id,
            actor_id=actor_id,
            name="Original",
            publisher="",
            match_rules=[{"type": "exact", "pattern": "Original", "publisher": None}],
        )
        await s.commit()
        product_id = product.id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(LicenseValidationError):
            await update_product(
                s,
                org_id=org_id,
                actor_id=actor_id,
                product_id=product_id,
                match_rules=[{"type": "regex", "pattern": "[", "publisher": None}],
            )


async def test_update_product_missing_raises_not_found(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(LicenseNotFoundError):
            await update_product(
                s, org_id=org_id, actor_id=actor_id, product_id=uuid4(), name="nope"
            )


async def test_get_and_list_product_write_no_audit(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        product = await create_product(
            s,
            org_id=org_id,
            actor_id=actor_id,
            name="Product",
            publisher="",
            match_rules=[],
        )
        await s.commit()
        product_id = product.id

    before = await _audit_actions(app_engine, org_id, "product", product_id)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await get_product(s, org_id, product_id)
        await list_products(s, org_id)

    after = await _audit_actions(app_engine, org_id, "product", product_id)
    assert after == before


async def test_another_orgs_product_is_hidden_by_rls(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, actor_b = await _org_with_user(admin_engine)

    async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as s:
        product = await create_product(
            s,
            org_id=org_a,
            actor_id=actor_a,
            name="Product A",
            publisher="",
            match_rules=[],
        )
        await s.commit()
        product_id = product.id

    async with org_session(org_b, engine=app_engine, clients=ClientScope.all()) as s:
        fetched = await get_product(s, org_b, product_id)
        assert fetched is None

        all_products = await list_products(s, org_b)
        assert all_products == []

        with pytest.raises(LicenseNotFoundError):
            await update_product(
                s, org_id=org_b, actor_id=actor_b, product_id=product_id, name="hijacked"
            )
