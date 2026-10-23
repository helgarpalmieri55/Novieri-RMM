"""Tests for the Licenses/SAM daily compliance sweep (docs/modules/licenses/
spec.md §7): the Celery-beat job that persists `license_compliance_snapshot`
rows per org, per product.

Mirrors `tests/test_licenses_reporting.py`'s fixture style (throwaway org/user
seeded via `admin_engine`, products via `create_product`, devices/installed
software seeded directly, licenses seeded via direct `License` model inserts)
and `tests/test_remote_retention_worker.py`'s all-orgs sweep shape.

Exercises `licenses_compliance_sweep_all_orgs` DIRECTLY (not via Celery). The
sweep processes EVERY org in the database (correct production behavior), and
the Postgres cluster is shared session-wide (`tests/conftest.py::pg_cluster`),
so other licenses test files leave their own orgs/products behind -- the sweep
picks those up too. The returned `total` is therefore asserted as `>= 3` (this
test seeds 2 products in org A + 1 in org B); exact correctness is proven by
the per-org, `org_id`-filtered snapshot assertions below, which are unaffected
by any other org's data.
"""

import datetime as _dt
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.config import Settings, override_settings
from app.core.tenancy import org_session
from app.models import License, LicenseComplianceSnapshot, Organization, User
from app.modules.licenses.service import create_product
from app.worker import licenses_compliance_sweep_all_orgs

# ---------------------------------------------------------------------------
# seeding helpers (mirrors tests/test_licenses_reporting.py)
# ---------------------------------------------------------------------------


async def _org_with_user(admin_engine: AsyncEngine) -> tuple[UUID, UUID]:
    slug = f"licswp-{uuid4().hex[:6]}"
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
    name: str,
    match_rules: list[dict],
):
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        product = await create_product(
            s,
            org_id=org_id,
            actor_id=actor_id,
            name=name,
            publisher="Acme Inc",
            match_rules=match_rules,
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
) -> UUID:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        lic = License(
            org_id=org_id,
            product_id=product_id,
            vendor_id=None,
            type="volume",
            entitlements=entitlements,
            cost=None,
            currency=None,
            po_ref=None,
            purchased_at=None,
            maintenance_expires_at=None,
            renews_at=None,
            agreement_ref=None,
            notes="",
            created_by=actor_id,
        )
        s.add(lic)
        await s.commit()
        return lic.id


# ---------------------------------------------------------------------------
# licenses_compliance_sweep_all_orgs
# ---------------------------------------------------------------------------


async def test_sweep_persists_snapshots_per_org_per_product_under_rls(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, pg_cluster, tmp_path: Path
) -> None:
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, actor_b = await _org_with_user(admin_engine)

    # Org A: 2 products.
    rules_1 = [{"type": "exact", "pattern": "App One", "publisher": None}]
    product_a1 = await _seed_product(
        app_engine, org_a, actor_a, name="A Product 1", match_rules=rules_1
    )
    rules_2 = [{"type": "exact", "pattern": "App Two", "publisher": None}]
    product_a2 = await _seed_product(
        app_engine, org_a, actor_a, name="A Product 2", match_rules=rules_2
    )

    await _seed_license(admin_engine, org_a, actor_a, product_a1.id, entitlements=5)
    await _seed_license(admin_engine, org_a, actor_a, product_a1.id, entitlements=3)
    await _seed_license(admin_engine, org_a, actor_a, product_a2.id, entitlements=4)

    a_dev1 = await _seed_device(admin_engine, org_a, "a-d1")
    a_dev2 = await _seed_device(admin_engine, org_a, "a-d2")
    a_dev3 = await _seed_device(admin_engine, org_a, "a-d3")
    await _seed_installed(admin_engine, org_a, a_dev1, "App One")
    await _seed_installed(admin_engine, org_a, a_dev2, "App One")
    await _seed_installed(admin_engine, org_a, a_dev3, "App One")
    await _seed_installed(admin_engine, org_a, a_dev1, "App Two")
    await _seed_installed(admin_engine, org_a, a_dev2, "App Two")
    # product_a1: entitled 8, installed 3, delta 5
    # product_a2: entitled 4, installed 2, delta 2

    # Org B: 1 product.
    rules_3 = [{"type": "exact", "pattern": "App Three", "publisher": None}]
    product_b = await _seed_product(
        app_engine, org_b, actor_b, name="B Product", match_rules=rules_3
    )
    await _seed_license(admin_engine, org_b, actor_b, product_b.id, entitlements=10)
    b_dev1 = await _seed_device(admin_engine, org_b, "b-d1")
    b_dev2 = await _seed_device(admin_engine, org_b, "b-d2")
    b_dev3 = await _seed_device(admin_engine, org_b, "b-d3")
    b_dev4 = await _seed_device(admin_engine, org_b, "b-d4")
    for dev in (b_dev1, b_dev2, b_dev3, b_dev4):
        await _seed_installed(admin_engine, org_b, dev, "App Three")
    # product_b: entitled 10, installed 4, delta 6

    fixed_now = _dt.datetime(2026, 7, 24, 3, 0, 0, tzinfo=_dt.UTC)

    settings = Settings(
        database_url=pg_cluster.app_url,
        database_admin_url=pg_cluster.admin_url,
        oidc_issuer="https://kc.example/realms/it-suite",
        oidc_jwks_url="https://kc.example/realms/it-suite/certs",
        ca_dir=tmp_path / "ca",
    )
    with override_settings(settings):
        total = await licenses_compliance_sweep_all_orgs(fixed_now)
    # The sweep covers every org in the (session-shared) DB, so other test
    # files' orgs contribute too; this test seeds 3 snapshots' worth. Exact
    # correctness is proven by the org-scoped assertions below.
    assert total >= 3

    async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as s:
        rows_a = (
            (
                await s.execute(
                    select(LicenseComplianceSnapshot).where(
                        LicenseComplianceSnapshot.org_id == org_a
                    )
                )
            )
            .scalars()
            .all()
        )

    assert len(rows_a) == 2
    by_product_a = {row.product_id: row for row in rows_a}
    assert by_product_a[product_a1.id].entitled == 8
    assert by_product_a[product_a1.id].installed == 3
    assert by_product_a[product_a1.id].delta == 5
    assert by_product_a[product_a1.id].computed_at == fixed_now
    assert by_product_a[product_a2.id].entitled == 4
    assert by_product_a[product_a2.id].installed == 2
    assert by_product_a[product_a2.id].delta == 2
    assert by_product_a[product_a2.id].computed_at == fixed_now

    async with org_session(org_b, engine=app_engine, clients=ClientScope.all()) as s:
        rows_b = (
            (
                await s.execute(
                    select(LicenseComplianceSnapshot).where(
                        LicenseComplianceSnapshot.org_id == org_b
                    )
                )
            )
            .scalars()
            .all()
        )

    assert len(rows_b) == 1
    row_b = rows_b[0]
    assert row_b.product_id == product_b.id
    assert row_b.entitled == 10
    assert row_b.installed == 4
    assert row_b.delta == 6
    assert row_b.computed_at == fixed_now

    # Org isolation: org A's snapshot count is unaffected by org B's data.
    assert len(rows_a) == 2
