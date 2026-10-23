from datetime import date
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.models import (
    License,
    LicenseAllocation,
    LicenseComplianceSnapshot,
    Organization,
    SoftwareIgnore,
    SoftwareProduct,
    User,
    Vendor,
)


async def _org_user(admin_engine: AsyncEngine):
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name="licenses", slug=f"licenses-{uuid4().hex[:6]}")
        s.add(org)
        await s.flush()
        usr = User(org_id=org.id, external_subject=f"s{uuid4().hex[:6]}", email="a@b.c")
        s.add(usr)
        await s.commit()
        return org.id, usr.id


async def test_licenses_models_roundtrip(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, user_id = await _org_user(admin_engine)
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        # Test Vendor
        vendor = Vendor(
            org_id=org_id,
            name="Acme Software",
            created_by=user_id,
        )
        s.add(vendor)
        await s.flush()
        assert vendor.website is None
        assert vendor.contacts == []

        # Test SoftwareProduct
        product = SoftwareProduct(
            org_id=org_id,
            name="Acme Suite",
            created_by=user_id,
        )
        s.add(product)
        await s.flush()
        assert product.publisher == ""
        assert product.match_rules == []

        # Test License
        lic = License(
            org_id=org_id,
            product_id=product.id,
            type="perpetual",
            entitlements=50,
            cost=Decimal("99.99"),
            created_by=user_id,
        )
        s.add(lic)
        await s.flush()
        assert lic.vendor_id is None
        assert lic.key_sealed is None
        assert lic.cost == Decimal("99.99")
        assert isinstance(lic.cost, Decimal)
        assert lic.currency is None
        assert lic.po_ref is None
        assert lic.purchased_at is None
        assert lic.maintenance_expires_at is None
        assert lic.renews_at is None
        assert lic.agreement_ref is None
        assert lic.notes == ""

        # Test License with vendor and dates set
        lic2 = License(
            org_id=org_id,
            product_id=product.id,
            vendor_id=vendor.id,
            type="subscription",
            entitlements=10,
            key_sealed="sealed-blob",
            cost=Decimal("1234.56"),
            currency="USD",
            po_ref="PO-1",
            purchased_at=date(2026, 1, 1),
            maintenance_expires_at=date(2027, 1, 1),
            renews_at=date(2027, 1, 1),
            agreement_ref="AGR-1",
            created_by=user_id,
        )
        s.add(lic2)
        await s.flush()
        assert lic2.vendor_id == vendor.id
        assert lic2.key_sealed == "sealed-blob"

        # Test LicenseAllocation
        alloc = LicenseAllocation(
            org_id=org_id,
            license_id=lic.id,
            target_type="device",
            target_id=uuid4(),
            created_by=user_id,
        )
        s.add(alloc)
        await s.flush()
        assert alloc.released_at is None

        # Test LicenseComplianceSnapshot
        snap = LicenseComplianceSnapshot(
            org_id=org_id,
            product_id=product.id,
            entitled=50,
            installed=40,
            delta=-10,
        )
        s.add(snap)
        await s.flush()
        assert snap.delta == -10

        # Test SoftwareIgnore
        ignore = SoftwareIgnore(
            org_id=org_id,
            name="Some Bundled Runtime",
            created_by=user_id,
        )
        s.add(ignore)
        await s.flush()

    # Verify round-trip with fresh session
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        got_vendor = (
            await s.execute(select(Vendor).where(Vendor.id == vendor.id))
        ).scalar_one()
        assert got_vendor.name == "Acme Software"
        assert got_vendor.contacts == []
        assert got_vendor.website is None

        got_product = (
            await s.execute(select(SoftwareProduct).where(SoftwareProduct.id == product.id))
        ).scalar_one()
        assert got_product.name == "Acme Suite"
        assert got_product.publisher == ""
        assert got_product.match_rules == []

        got_lic = (
            await s.execute(select(License).where(License.id == lic.id))
        ).scalar_one()
        assert got_lic.entitlements == 50
        assert got_lic.cost == Decimal("99.99")
        assert isinstance(got_lic.cost, Decimal)
        assert got_lic.key_sealed is None
        assert got_lic.vendor_id is None
        assert got_lic.notes == ""

        got_lic2 = (
            await s.execute(select(License).where(License.id == lic2.id))
        ).scalar_one()
        assert got_lic2.vendor_id == vendor.id
        assert got_lic2.key_sealed == "sealed-blob"
        assert got_lic2.currency == "USD"
        assert got_lic2.purchased_at == date(2026, 1, 1)

        got_alloc = (
            await s.execute(
                select(LicenseAllocation).where(LicenseAllocation.id == alloc.id)
            )
        ).scalar_one()
        assert got_alloc.target_type == "device"
        assert got_alloc.released_at is None

        got_snap = (
            await s.execute(
                select(LicenseComplianceSnapshot).where(
                    LicenseComplianceSnapshot.id == snap.id
                )
            )
        ).scalar_one()
        assert got_snap.entitled == 50
        assert got_snap.installed == 40
        assert got_snap.delta == -10

        got_ignore = (
            await s.execute(select(SoftwareIgnore).where(SoftwareIgnore.id == ignore.id))
        ).scalar_one()
        assert got_ignore.name == "Some Bundled Runtime"
