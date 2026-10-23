"""Tests for the Licenses/SAM reporting module (docs/modules/licenses/spec.md
§8): compliance summary, renewal calendar, cost view, and CSV export rows.

Mirrors `tests/test_licenses_matching.py`'s fixture style (throwaway org/user
seeded via `admin_engine`, products via `create_product`, devices/installed
software seeded directly) and `tests/test_bcp_reporting.py`'s
`test_tenancy_report_org_filters_are_load_bearing` pattern: run every report
on an RLS-bypassing admin session with a second org's data present, so each
report function's own `org_id` predicate -- not RLS -- is what's proven to
exclude it.

Licenses are seeded via direct `License` model inserts (no vault needed here
-- these tests never touch `key`/`key_sealed`, which is the point: reporting
never surfaces them).
"""

import datetime as _dt
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import License, Organization, User
from app.modules.licenses.reporting import (
    compliance_summary,
    cost_view,
    export_rows,
    renewal_calendar,
)
from app.modules.licenses.service import create_product

# ---------------------------------------------------------------------------
# seeding helpers
# ---------------------------------------------------------------------------


async def _org_with_user(admin_engine: AsyncEngine) -> tuple[UUID, UUID]:
    slug = f"licrpt-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name=slug, slug=slug)
        s.add(org)
        await s.flush()
        user = User(org_id=org.id, external_subject=f"sub-{slug}", email=f"{slug}@example.com")
        s.add(user)
        await s.commit()
        return org.id, user.id


async def _seed_product(
    app_engine: AsyncEngine,
    org_id: UUID,
    actor_id: UUID,
    *,
    name: str = "Acme Suite",
    match_rules: list[dict] | None = None,
):
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        product = await create_product(
            s,
            org_id=org_id,
            actor_id=actor_id,
            name=name,
            publisher="Acme Inc",
            match_rules=match_rules if match_rules is not None else [],
        )
        await s.commit()
        return product


async def _seed_device(admin_engine: AsyncEngine, org_id: UUID, hostname: str) -> UUID:
    from app.models import Device

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
) -> None:
    from app.models import InstalledSoftware

    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        row = InstalledSoftware(org_id=org_id, device_id=device_id, name=name, publisher=publisher)
        s.add(row)
        await s.commit()


async def _seed_license(
    admin_engine: AsyncEngine,
    org_id: UUID,
    actor_id: UUID,
    product_id: UUID,
    *,
    entitlements: int,
    type_: str = "volume",
    cost: Decimal | None = None,
    currency: str | None = None,
    purchased_at: _dt.date | None = None,
    maintenance_expires_at: _dt.date | None = None,
    renews_at: _dt.date | None = None,
    po_ref: str | None = None,
    notes: str = "",
) -> UUID:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        lic = License(
            org_id=org_id,
            product_id=product_id,
            vendor_id=None,
            type=type_,
            entitlements=entitlements,
            cost=cost,
            currency=currency,
            po_ref=po_ref,
            purchased_at=purchased_at,
            maintenance_expires_at=maintenance_expires_at,
            renews_at=renews_at,
            agreement_ref=None,
            notes=notes,
            created_by=actor_id,
        )
        s.add(lic)
        await s.commit()
        return lic.id


# ---------------------------------------------------------------------------
# compliance_summary
# ---------------------------------------------------------------------------


async def test_compliance_summary_hand_computed(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    match_rules = [{"type": "exact", "pattern": "Acme App", "publisher": None}]
    product = await _seed_product(app_engine, org_id, actor_id, match_rules=match_rules)

    await _seed_license(admin_engine, org_id, actor_id, product.id, entitlements=5)
    await _seed_license(admin_engine, org_id, actor_id, product.id, entitlements=3)

    device1 = await _seed_device(admin_engine, org_id, "d1")
    device2 = await _seed_device(admin_engine, org_id, "d2")
    device3 = await _seed_device(admin_engine, org_id, "d3")
    await _seed_installed(admin_engine, org_id, device1, "Acme App")
    await _seed_installed(admin_engine, org_id, device2, "Acme App")
    await _seed_installed(admin_engine, org_id, device3, "Acme App")

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        rows = await compliance_summary(s, org_id)

    assert len(rows) == 1
    row = rows[0]
    assert row["product_id"] == product.id
    assert row["name"] == "Acme Suite"
    assert row["entitled"] == 8
    assert row["installed"] == 3
    assert row["delta"] == 5
    assert row["breach"] is False


async def test_compliance_summary_breach_when_installed_exceeds_entitled(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    match_rules = [{"type": "exact", "pattern": "Solo App", "publisher": None}]
    product = await _seed_product(
        app_engine, org_id, actor_id, name="Solo Product", match_rules=match_rules
    )

    await _seed_license(admin_engine, org_id, actor_id, product.id, entitlements=1)

    device1 = await _seed_device(admin_engine, org_id, "d1")
    device2 = await _seed_device(admin_engine, org_id, "d2")
    await _seed_installed(admin_engine, org_id, device1, "Solo App")
    await _seed_installed(admin_engine, org_id, device2, "Solo App")

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        rows = await compliance_summary(s, org_id)

    row = next(r for r in rows if r["product_id"] == product.id)
    assert row["entitled"] == 1
    assert row["installed"] == 2
    assert row["delta"] == -1
    assert row["breach"] is True


async def test_compliance_summary_product_with_no_licenses_is_zero_entitled(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    product = await _seed_product(app_engine, org_id, actor_id, name="No License Product")

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        rows = await compliance_summary(s, org_id)

    row = next(r for r in rows if r["product_id"] == product.id)
    assert row["entitled"] == 0
    assert row["installed"] == 0
    assert row["delta"] == 0
    assert row["breach"] is False


# ---------------------------------------------------------------------------
# renewal_calendar
# ---------------------------------------------------------------------------


async def test_renewal_calendar_hand_computed(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    product = await _seed_product(app_engine, org_id, actor_id)

    now = _dt.datetime(2026, 1, 1, tzinfo=_dt.UTC)

    # renews in 10 days -- included
    lic_soon = await _seed_license(
        admin_engine,
        org_id,
        actor_id,
        product.id,
        entitlements=1,
        renews_at=_dt.date(2026, 1, 11),
    )
    # renews in 60 days -- excluded (outside default 30-day window)
    await _seed_license(
        admin_engine,
        org_id,
        actor_id,
        product.id,
        entitlements=1,
        renews_at=_dt.date(2026, 3, 2),
    )
    # maintenance expires in 5 days -- included, trigger == "maintenance"
    lic_maint = await _seed_license(
        admin_engine,
        org_id,
        actor_id,
        product.id,
        entitlements=1,
        maintenance_expires_at=_dt.date(2026, 1, 6),
    )
    # BOTH dates qualify -- emits TWO rows
    lic_both = await _seed_license(
        admin_engine,
        org_id,
        actor_id,
        product.id,
        entitlements=1,
        renews_at=_dt.date(2026, 1, 20),
        maintenance_expires_at=_dt.date(2026, 1, 15),
    )

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        rows = await renewal_calendar(s, org_id, now=now)

    by_license: dict[UUID, list[dict]] = {}
    for row in rows:
        by_license.setdefault(row["license_id"], []).append(row)

    assert lic_soon in by_license
    soon_row = by_license[lic_soon][0]
    assert soon_row["trigger"] == "renewal"
    assert soon_row["due_in_days"] == 10
    assert soon_row["due_date"] == _dt.date(2026, 1, 11)

    assert lic_maint in by_license
    maint_row = by_license[lic_maint][0]
    assert maint_row["trigger"] == "maintenance"
    assert maint_row["due_in_days"] == 5

    assert len(by_license[lic_both]) == 2
    triggers = {row["trigger"] for row in by_license[lic_both]}
    assert triggers == {"renewal", "maintenance"}

    # the 60-day renewal must not appear at all
    all_license_ids = {row["license_id"] for row in rows}
    assert all(row["due_date"] != _dt.date(2026, 3, 2) for row in rows)
    assert len(all_license_ids) == 3

    # sorted by due_date ascending
    due_dates = [row["due_date"] for row in rows]
    assert due_dates == sorted(due_dates)


async def test_renewal_calendar_within_days_boundary_is_inclusive(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    product = await _seed_product(app_engine, org_id, actor_id)
    now = _dt.datetime(2026, 1, 1, tzinfo=_dt.UTC)

    lic_boundary = await _seed_license(
        admin_engine,
        org_id,
        actor_id,
        product.id,
        entitlements=1,
        renews_at=_dt.date(2026, 1, 31),  # exactly 30 days out
    )

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        rows = await renewal_calendar(s, org_id, now=now, within_days=30)

    assert any(row["license_id"] == lic_boundary and row["due_in_days"] == 30 for row in rows)


# ---------------------------------------------------------------------------
# cost_view
# ---------------------------------------------------------------------------


async def test_cost_view_groups_by_product_and_currency_no_cross_currency_sum(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    product = await _seed_product(app_engine, org_id, actor_id)

    await _seed_license(
        admin_engine,
        org_id,
        actor_id,
        product.id,
        entitlements=1,
        cost=Decimal("100"),
        currency="USD",
    )
    await _seed_license(
        admin_engine,
        org_id,
        actor_id,
        product.id,
        entitlements=1,
        cost=Decimal("50"),
        currency="USD",
    )
    await _seed_license(
        admin_engine,
        org_id,
        actor_id,
        product.id,
        entitlements=1,
        cost=Decimal("40"),
        currency="EUR",
    )
    # NULL cost / NULL currency licenses are skipped entirely
    await _seed_license(
        admin_engine, org_id, actor_id, product.id, entitlements=1, cost=None, currency=None
    )
    await _seed_license(
        admin_engine,
        org_id,
        actor_id,
        product.id,
        entitlements=1,
        cost=Decimal("10"),
        currency=None,
    )

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        rows = await cost_view(s, org_id)

    by_currency = {row["currency"]: row for row in rows}
    assert set(by_currency) == {"USD", "EUR"}
    assert by_currency["USD"]["total_cost"] == Decimal("150")
    assert by_currency["USD"]["product_id"] == product.id
    assert by_currency["EUR"]["total_cost"] == Decimal("40")


# ---------------------------------------------------------------------------
# export_rows
# ---------------------------------------------------------------------------


async def test_export_rows_has_no_key_material_and_carries_expected_fields(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    product = await _seed_product(app_engine, org_id, actor_id, name="Export Product")

    license_id = await _seed_license(
        admin_engine,
        org_id,
        actor_id,
        product.id,
        entitlements=7,
        cost=Decimal("200"),
        currency="USD",
        po_ref="PO-1",
        purchased_at=_dt.date(2025, 6, 1),
        notes="some notes",
    )

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        rows = await export_rows(s, org_id)

    assert len(rows) == 1
    row = rows[0]
    assert "key" not in row
    assert "key_sealed" not in row
    assert row["license_id"] == license_id
    assert row["product_id"] == product.id
    assert row["product_name"] == "Export Product"
    assert row["entitlements"] == 7
    assert row["cost"] == Decimal("200")
    assert row["currency"] == "USD"
    assert row["po_ref"] == "PO-1"
    assert row["purchased_at"] == _dt.date(2025, 6, 1)
    assert row["notes"] == "some notes"


async def test_export_rows_ordered_deterministically_by_product_name(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    product_z = await _seed_product(app_engine, org_id, actor_id, name="Zeta")
    product_a = await _seed_product(app_engine, org_id, actor_id, name="Alpha")

    await _seed_license(admin_engine, org_id, actor_id, product_z.id, entitlements=1)
    await _seed_license(admin_engine, org_id, actor_id, product_a.id, entitlements=1)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        rows = await export_rows(s, org_id)

    names = [row["product_name"] for row in rows]
    assert names == ["Alpha", "Zeta"]


# ---------------------------------------------------------------------------
# tenancy
# ---------------------------------------------------------------------------


async def test_a_second_orgs_rows_are_excluded_from_all_four_reports_by_rls(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, actor_b = await _org_with_user(admin_engine)

    match_rules = [{"type": "exact", "pattern": "Shared App", "publisher": None}]
    product_a = await _seed_product(
        app_engine, org_a, actor_a, name="A Product", match_rules=match_rules
    )
    product_b = await _seed_product(
        app_engine, org_b, actor_b, name="B Product", match_rules=match_rules
    )

    device_a = await _seed_device(admin_engine, org_a, "device-a")
    device_b = await _seed_device(admin_engine, org_b, "device-b")
    await _seed_installed(admin_engine, org_a, device_a, "Shared App")
    await _seed_installed(admin_engine, org_b, device_b, "Shared App")

    await _seed_license(admin_engine, org_a, actor_a, product_a.id, entitlements=2)
    await _seed_license(
        admin_engine,
        org_b,
        actor_b,
        product_b.id,
        entitlements=99,
        cost=Decimal("999"),
        currency="USD",
    )

    now = _dt.datetime(2026, 1, 1, tzinfo=_dt.UTC)
    await _seed_license(
        admin_engine, org_a, actor_a, product_a.id, entitlements=1, renews_at=_dt.date(2026, 1, 5)
    )
    await _seed_license(
        admin_engine, org_b, actor_b, product_b.id, entitlements=1, renews_at=_dt.date(2026, 1, 5)
    )

    async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as s:
        compliance = await compliance_summary(s, org_a)
        renewals = await renewal_calendar(s, org_a, now=now)
        costs = await cost_view(s, org_a)
        export = await export_rows(s, org_a)

    assert {row["product_id"] for row in compliance} == {product_a.id}
    assert all(row["product_id"] == product_a.id for row in renewals)
    assert all(row["product_id"] == product_a.id for row in costs)
    assert all(row["product_id"] == product_a.id for row in export)
    assert product_b.id not in {row["product_id"] for row in compliance}


async def test_tenancy_report_org_filters_are_load_bearing(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    """Run `compliance_summary` on an RLS-bypassing admin session (the
    `admin_engine` role bypasses RLS -- it is how the cross-org seed helpers
    insert) with a second org's product/license/install present, so the
    explicit `org_id` predicates inside `compliance_summary` (and the
    `product_installed_device_ids` it calls) are the SOLE guard against
    cross-tenant leakage -- RLS is not backstopping it here. Mirrors
    `tests/test_bcp_reporting.py::test_tenancy_report_org_filters_are_load_bearing`.
    """
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, actor_b = await _org_with_user(admin_engine)

    match_rules = [{"type": "exact", "pattern": "Shared App", "publisher": None}]
    product_a = await _seed_product(
        app_engine, org_a, actor_a, name="A Product", match_rules=match_rules
    )
    product_b = await _seed_product(
        app_engine, org_b, actor_b, name="B Product", match_rules=match_rules
    )

    device_a = await _seed_device(admin_engine, org_a, "device-a")
    device_b1 = await _seed_device(admin_engine, org_b, "device-b1")
    device_b2 = await _seed_device(admin_engine, org_b, "device-b2")
    await _seed_installed(admin_engine, org_a, device_a, "Shared App")
    await _seed_installed(admin_engine, org_b, device_b1, "Shared App")
    await _seed_installed(admin_engine, org_b, device_b2, "Shared App")

    await _seed_license(admin_engine, org_a, actor_a, product_a.id, entitlements=5)
    # org B: entitled 1, installed 2 -- would read as a breach if leaked into org A's rollup
    await _seed_license(admin_engine, org_b, actor_b, product_b.id, entitlements=1)

    # RLS-bypassing admin session: org B's rows are visible on this
    # connection, so only compliance_summary's own org_id predicates (and
    # product_installed_device_ids') can exclude them.
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        rows = await compliance_summary(s, org_a)

    assert {row["product_id"] for row in rows} == {product_a.id}
    row = rows[0]
    assert row["entitled"] == 5
    assert row["installed"] == 1
    assert row["breach"] is False
