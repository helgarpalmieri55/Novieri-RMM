"""Tests for the Licenses/SAM license service (docs/modules/licenses/spec.md
§5): vault-sealed keys and an audited reveal.

Mirrors `tests/test_licenses_vendor.py`'s fixture style (throwaway org + user
seeded via `admin_engine`, service calls through an RLS-constrained
`org_session`, audit rows read back by `target_type`/`target_id`) plus
`tests/test_vault_bytes.py`'s `LocalVaultService(tmp_path / "root.key")`
fixture for the vault.

Security-sensitive (invariant #3: keys never in plaintext in DB/logs/
responses; invariant #4: reveal is audited) — every test here either asserts
the sealed form is not the plaintext, or asserts the audit trail is exactly
right.
"""

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.tenancy import org_session
from app.core.vault import LocalVaultService
from app.models import AuditEvent, Organization, SoftwareProduct, User, Vendor
from app.modules.licenses.errors import LicenseNotFoundError, LicenseValidationError
from app.modules.licenses.models import License
from app.modules.licenses.service import (
    create_license,
    delete_license,
    get_license,
    list_licenses,
    reveal_key,
    update_license,
)


async def _org_with_user(admin_engine: AsyncEngine) -> tuple[UUID, UUID]:
    slug = f"licsvc-{uuid4().hex[:6]}"
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        org = Organization(name=slug, slug=slug)
        s.add(org)
        await s.flush()
        user = User(org_id=org.id, external_subject=f"sub-{slug}", email=f"{slug}@example.com")
        s.add(user)
        await s.commit()
        return org.id, user.id


async def _product(admin_engine: AsyncEngine, org_id: UUID, actor_id: UUID) -> UUID:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        product = SoftwareProduct(
            org_id=org_id, name="Product", publisher="Pub", match_rules=[], created_by=actor_id
        )
        s.add(product)
        await s.commit()
        return product.id


async def _vendor(admin_engine: AsyncEngine, org_id: UUID, actor_id: UUID) -> UUID:
    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        vendor = Vendor(
            org_id=org_id, name="Vendor", website=None, contacts=[], created_by=actor_id
        )
        s.add(vendor)
        await s.commit()
        return vendor.id


def _vault(tmp_path: Path) -> LocalVaultService:
    return LocalVaultService(tmp_path / "root.key")


async def _audit_actions(
    app_engine: AsyncEngine, org_id: UUID, target_type: str, target_id: UUID
) -> list[str]:
    async with org_session(org_id, engine=app_engine) as s:
        events = (
            await s.execute(
                select(AuditEvent)
                .where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.target_type == target_type,
                    AuditEvent.target_id == str(target_id),
                )
                .order_by(AuditEvent.id)
            )
        ).scalars().all()
        return [e.action for e in events]


# ---------------------------------------------------------------------------
# create_license
# ---------------------------------------------------------------------------


async def test_create_license_with_key_seals_it_and_audits(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    product_id = await _product(admin_engine, org_id, actor_id)
    vault = _vault(tmp_path)
    plaintext_key = "XXXX-YYYY-ZZZZ-1234"

    async with org_session(org_id, engine=app_engine) as s:
        license_ = await create_license(
            s,
            vault,
            org_id=org_id,
            actor_id=actor_id,
            product_id=product_id,
            vendor_id=None,
            type_="perpetual",
            entitlements=10,
            key=plaintext_key,
            cost=None,
            currency=None,
            po_ref=None,
            purchased_at=None,
            maintenance_expires_at=None,
            renews_at=None,
            agreement_ref=None,
            notes="",
        )
        await s.commit()
        license_id = license_.id

    assert license_.key_sealed is not None
    assert isinstance(license_.key_sealed, str)
    assert license_.key_sealed != plaintext_key
    assert plaintext_key not in license_.key_sealed

    async with org_session(org_id, engine=app_engine) as s:
        fetched = await get_license(s, org_id, license_id)
        assert fetched is not None
        # ORM-level access exposes key_sealed (there's no response mapping in
        # this service layer) -- but it is never the plaintext.
        assert fetched.key_sealed is not None
        assert fetched.key_sealed != plaintext_key

    async with org_session(org_id, engine=app_engine) as s:
        revealed = await reveal_key(
            s, vault, org_id=org_id, actor_id=actor_id, license_id=license_id
        )
        await s.commit()
        assert revealed == plaintext_key

    actions = await _audit_actions(app_engine, org_id, "license", license_id)
    assert actions == ["license.created", "license.key_revealed"]

    async with org_session(org_id, engine=app_engine) as s:
        created_event = (
            await s.execute(
                select(AuditEvent)
                .where(
                    AuditEvent.org_id == org_id, AuditEvent.action == "license.created"
                )
            )
        ).scalar_one()
        assert created_event.meta["product_id"] == str(product_id)
        assert created_event.meta["entitlements"] == 10
        assert "key" not in created_event.meta
        assert plaintext_key not in str(created_event.meta)

        revealed_event = (
            await s.execute(
                select(AuditEvent)
                .where(
                    AuditEvent.org_id == org_id, AuditEvent.action == "license.key_revealed"
                )
            )
        ).scalar_one()
        assert revealed_event.target_type == "license"
        assert revealed_event.target_id == str(license_id)
        assert revealed_event.meta == {}


async def test_create_license_without_key_leaves_key_sealed_none(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    product_id = await _product(admin_engine, org_id, actor_id)
    vault = _vault(tmp_path)

    async with org_session(org_id, engine=app_engine) as s:
        license_ = await create_license(
            s,
            vault,
            org_id=org_id,
            actor_id=actor_id,
            product_id=product_id,
            vendor_id=None,
            type_="volume",
            entitlements=5,
            key=None,
            cost=None,
            currency=None,
            po_ref=None,
            purchased_at=None,
            maintenance_expires_at=None,
            renews_at=None,
            agreement_ref=None,
            notes="",
        )
        await s.commit()
        assert license_.key_sealed is None


async def test_create_license_cross_org_product_raises_validation_error(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, actor_b = await _org_with_user(admin_engine)
    product_b = await _product(admin_engine, org_b, actor_b)
    vault = _vault(tmp_path)

    async with org_session(org_a, engine=app_engine) as s:
        with pytest.raises(LicenseValidationError):
            await create_license(
                s,
                vault,
                org_id=org_a,
                actor_id=actor_a,
                product_id=product_b,
                vendor_id=None,
                type_="perpetual",
                entitlements=1,
                key=None,
                cost=None,
                currency=None,
                po_ref=None,
                purchased_at=None,
                maintenance_expires_at=None,
                renews_at=None,
                agreement_ref=None,
                notes="",
            )


async def test_create_license_cross_org_vendor_raises_validation_error(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, actor_b = await _org_with_user(admin_engine)
    product_a = await _product(admin_engine, org_a, actor_a)
    vendor_b = await _vendor(admin_engine, org_b, actor_b)
    vault = _vault(tmp_path)

    async with org_session(org_a, engine=app_engine) as s:
        with pytest.raises(LicenseValidationError):
            await create_license(
                s,
                vault,
                org_id=org_a,
                actor_id=actor_a,
                product_id=product_a,
                vendor_id=vendor_b,
                type_="perpetual",
                entitlements=1,
                key=None,
                cost=None,
                currency=None,
                po_ref=None,
                purchased_at=None,
                maintenance_expires_at=None,
                renews_at=None,
                agreement_ref=None,
                notes="",
            )


# ---------------------------------------------------------------------------
# reveal_key
# ---------------------------------------------------------------------------


async def test_reveal_key_on_keyless_license_raises_validation_error(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    product_id = await _product(admin_engine, org_id, actor_id)
    vault = _vault(tmp_path)

    async with org_session(org_id, engine=app_engine) as s:
        license_ = await create_license(
            s,
            vault,
            org_id=org_id,
            actor_id=actor_id,
            product_id=product_id,
            vendor_id=None,
            type_="perpetual",
            entitlements=1,
            key=None,
            cost=None,
            currency=None,
            po_ref=None,
            purchased_at=None,
            maintenance_expires_at=None,
            renews_at=None,
            agreement_ref=None,
            notes="",
        )
        await s.commit()
        license_id = license_.id

    async with org_session(org_id, engine=app_engine) as s:
        with pytest.raises(LicenseValidationError):
            await reveal_key(s, vault, org_id=org_id, actor_id=actor_id, license_id=license_id)


# ---------------------------------------------------------------------------
# update_license
# ---------------------------------------------------------------------------


async def test_update_license_key_empty_string_clears_key(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    product_id = await _product(admin_engine, org_id, actor_id)
    vault = _vault(tmp_path)

    async with org_session(org_id, engine=app_engine) as s:
        license_ = await create_license(
            s,
            vault,
            org_id=org_id,
            actor_id=actor_id,
            product_id=product_id,
            vendor_id=None,
            type_="perpetual",
            entitlements=1,
            key="orig-key",
            cost=None,
            currency=None,
            po_ref=None,
            purchased_at=None,
            maintenance_expires_at=None,
            renews_at=None,
            agreement_ref=None,
            notes="",
        )
        await s.commit()
        license_id = license_.id

    async with org_session(org_id, engine=app_engine) as s:
        updated = await update_license(
            s, vault, org_id=org_id, actor_id=actor_id, license_id=license_id, key=""
        )
        await s.commit()
        assert updated.key_sealed is None

    async with org_session(org_id, engine=app_engine) as s:
        with pytest.raises(LicenseValidationError):
            await reveal_key(s, vault, org_id=org_id, actor_id=actor_id, license_id=license_id)

    actions = await _audit_actions(app_engine, org_id, "license", license_id)
    assert actions == ["license.created", "license.updated"]


async def test_update_license_new_key_reseals_and_reveals_new_plaintext(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    product_id = await _product(admin_engine, org_id, actor_id)
    vault = _vault(tmp_path)

    async with org_session(org_id, engine=app_engine) as s:
        license_ = await create_license(
            s,
            vault,
            org_id=org_id,
            actor_id=actor_id,
            product_id=product_id,
            vendor_id=None,
            type_="perpetual",
            entitlements=1,
            key="orig-key",
            cost=None,
            currency=None,
            po_ref=None,
            purchased_at=None,
            maintenance_expires_at=None,
            renews_at=None,
            agreement_ref=None,
            notes="",
        )
        await s.commit()
        license_id = license_.id

    async with org_session(org_id, engine=app_engine) as s:
        await update_license(
            s, vault, org_id=org_id, actor_id=actor_id, license_id=license_id, key="new-key"
        )
        await s.commit()

    async with org_session(org_id, engine=app_engine) as s:
        revealed = await reveal_key(
            s, vault, org_id=org_id, actor_id=actor_id, license_id=license_id
        )
        assert revealed == "new-key"


async def test_update_license_key_absent_leaves_existing_key_untouched(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    product_id = await _product(admin_engine, org_id, actor_id)
    vault = _vault(tmp_path)

    async with org_session(org_id, engine=app_engine) as s:
        license_ = await create_license(
            s,
            vault,
            org_id=org_id,
            actor_id=actor_id,
            product_id=product_id,
            vendor_id=None,
            type_="perpetual",
            entitlements=1,
            key="orig-key",
            cost=None,
            currency=None,
            po_ref=None,
            purchased_at=None,
            maintenance_expires_at=None,
            renews_at=None,
            agreement_ref=None,
            notes="",
        )
        await s.commit()
        license_id = license_.id

    async with org_session(org_id, engine=app_engine) as s:
        await update_license(
            s, vault, org_id=org_id, actor_id=actor_id, license_id=license_id, notes="updated notes"
        )
        await s.commit()

    async with org_session(org_id, engine=app_engine) as s:
        revealed = await reveal_key(
            s, vault, org_id=org_id, actor_id=actor_id, license_id=license_id
        )
        assert revealed == "orig-key"


async def test_update_license_explicit_key_none_leaves_existing_key_untouched(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    # The API route calls update_license(**LicenseUpdate.model_dump()), which
    # passes key=None EXPLICITLY (present-but-None), a different branch from the
    # key-absent case above. Accidental key-clearing here would be catastrophic
    # (invariant #3), so assert the existing sealed key survives an explicit
    # key=None update.
    org_id, actor_id = await _org_with_user(admin_engine)
    product_id = await _product(admin_engine, org_id, actor_id)
    vault = _vault(tmp_path)

    async with org_session(org_id, engine=app_engine) as s:
        license_ = await create_license(
            s,
            vault,
            org_id=org_id,
            actor_id=actor_id,
            product_id=product_id,
            vendor_id=None,
            type_="perpetual",
            entitlements=1,
            key="orig-key",
            cost=None,
            currency=None,
            po_ref=None,
            purchased_at=None,
            maintenance_expires_at=None,
            renews_at=None,
            agreement_ref=None,
            notes="",
        )
        await s.commit()
        license_id = license_.id

    async with org_session(org_id, engine=app_engine) as s:
        await update_license(
            s,
            vault,
            org_id=org_id,
            actor_id=actor_id,
            license_id=license_id,
            key=None,
            notes="updated notes",
        )
        await s.commit()

    async with org_session(org_id, engine=app_engine) as s:
        revealed = await reveal_key(
            s, vault, org_id=org_id, actor_id=actor_id, license_id=license_id
        )
        assert revealed == "orig-key"


async def test_update_license_cross_org_product_raises_validation_error(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, actor_b = await _org_with_user(admin_engine)
    product_a = await _product(admin_engine, org_a, actor_a)
    product_b = await _product(admin_engine, org_b, actor_b)
    vault = _vault(tmp_path)

    async with org_session(org_a, engine=app_engine) as s:
        license_ = await create_license(
            s,
            vault,
            org_id=org_a,
            actor_id=actor_a,
            product_id=product_a,
            vendor_id=None,
            type_="perpetual",
            entitlements=1,
            key=None,
            cost=None,
            currency=None,
            po_ref=None,
            purchased_at=None,
            maintenance_expires_at=None,
            renews_at=None,
            agreement_ref=None,
            notes="",
        )
        await s.commit()
        license_id = license_.id

    async with org_session(org_a, engine=app_engine) as s:
        with pytest.raises(LicenseValidationError):
            await update_license(
                s,
                vault,
                org_id=org_a,
                actor_id=actor_a,
                license_id=license_id,
                product_id=product_b,
            )


# ---------------------------------------------------------------------------
# delete_license
# ---------------------------------------------------------------------------


async def test_delete_license_removes_row_and_audits(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    product_id = await _product(admin_engine, org_id, actor_id)
    vault = _vault(tmp_path)

    async with org_session(org_id, engine=app_engine) as s:
        license_ = await create_license(
            s,
            vault,
            org_id=org_id,
            actor_id=actor_id,
            product_id=product_id,
            vendor_id=None,
            type_="perpetual",
            entitlements=1,
            key=None,
            cost=None,
            currency=None,
            po_ref=None,
            purchased_at=None,
            maintenance_expires_at=None,
            renews_at=None,
            agreement_ref=None,
            notes="",
        )
        await s.commit()
        license_id = license_.id

    async with org_session(org_id, engine=app_engine) as s:
        await delete_license(s, org_id=org_id, actor_id=actor_id, license_id=license_id)
        await s.commit()

    async with org_session(org_id, engine=app_engine) as s:
        fetched = await get_license(s, org_id, license_id)
        assert fetched is None

    actions = await _audit_actions(app_engine, org_id, "license", license_id)
    assert actions == ["license.created", "license.deleted"]


async def test_delete_license_missing_raises_not_found(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    async with org_session(org_id, engine=app_engine) as s:
        with pytest.raises(LicenseNotFoundError):
            await delete_license(s, org_id=org_id, actor_id=actor_id, license_id=uuid4())


# ---------------------------------------------------------------------------
# get_license / list_licenses -- no audit
# ---------------------------------------------------------------------------


async def test_get_and_list_license_write_no_audit(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    product_id = await _product(admin_engine, org_id, actor_id)
    vault = _vault(tmp_path)

    async with org_session(org_id, engine=app_engine) as s:
        license_ = await create_license(
            s,
            vault,
            org_id=org_id,
            actor_id=actor_id,
            product_id=product_id,
            vendor_id=None,
            type_="perpetual",
            entitlements=1,
            key=None,
            cost=None,
            currency=None,
            po_ref=None,
            purchased_at=None,
            maintenance_expires_at=None,
            renews_at=None,
            agreement_ref=None,
            notes="",
        )
        await s.commit()
        license_id = license_.id

    before = await _audit_actions(app_engine, org_id, "license", license_id)

    async with org_session(org_id, engine=app_engine) as s:
        await get_license(s, org_id, license_id)
        all_licenses = await list_licenses(s, org_id)
        assert [lic.id for lic in all_licenses] == [license_id]

    after = await _audit_actions(app_engine, org_id, "license", license_id)
    assert after == before


async def test_get_license_missing_returns_none(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, _actor_id = await _org_with_user(admin_engine)
    async with org_session(org_id, engine=app_engine) as s:
        result = await get_license(s, org_id, uuid4())
        assert result is None


# ---------------------------------------------------------------------------
# tenancy
# ---------------------------------------------------------------------------


async def test_another_orgs_license_is_hidden_by_rls(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, actor_b = await _org_with_user(admin_engine)
    product_a = await _product(admin_engine, org_a, actor_a)
    vault = _vault(tmp_path)

    async with org_session(org_a, engine=app_engine) as s:
        license_ = await create_license(
            s,
            vault,
            org_id=org_a,
            actor_id=actor_a,
            product_id=product_a,
            vendor_id=None,
            type_="perpetual",
            entitlements=1,
            key="a-key",
            cost=None,
            currency=None,
            po_ref=None,
            purchased_at=None,
            maintenance_expires_at=None,
            renews_at=None,
            agreement_ref=None,
            notes="",
        )
        await s.commit()
        license_id = license_.id

    async with org_session(org_b, engine=app_engine) as s:
        fetched = await get_license(s, org_b, license_id)
        assert fetched is None

        all_licenses = await list_licenses(s, org_b)
        assert all_licenses == []

        with pytest.raises(LicenseNotFoundError):
            await update_license(
                s, vault, org_id=org_b, actor_id=actor_b, license_id=license_id, notes="hijacked"
            )

        with pytest.raises(LicenseNotFoundError):
            await reveal_key(s, vault, org_id=org_b, actor_id=actor_b, license_id=license_id)

        with pytest.raises(LicenseNotFoundError):
            await delete_license(s, org_id=org_b, actor_id=actor_b, license_id=license_id)


# ---------------------------------------------------------------------------
# The org predicate, where RLS is not the thing rejecting
# ---------------------------------------------------------------------------


async def test_the_license_org_predicate_holds_where_rls_is_not_doing_the_rejecting(
    admin_engine: AsyncEngine, bypassrls_session: AsyncSession
) -> None:
    """THE TEST IN THIS FILE THAT CAN ACTUALLY FAIL.

    `test_another_orgs_license_is_hidden_by_rls` above runs every call through
    `org_session` on the `app_user` role, where Postgres rejects org B's rows
    before `License.org_id == org_id` is ever consulted -- so it stays green
    with that predicate deleted, and what it pins is the shape of the refusal
    (`None`, `[]`, `LicenseNotFoundError`) rather than the filter.

    Here `its_admin` carries BYPASSRLS (`tests/conftest.py`), so the SQL is the
    only thing scoping anything and the module's belt is observable
    independently of the braces. All three read shapes are covered because they
    are three separate predicates: the single-row `get`, the whole-table
    `list`, and `_load_license`, which is what every write path resolves
    through.

    FALSIFIED: deleting `License.org_id == org_id` from `get_license` turns the
    first assertion into org B's license; deleting it from `list_licenses`
    turns the second into both orgs' rows; deleting it from `_load_license`
    lets `delete_license` destroy a neighbour's row.
    """
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, actor_b = await _org_with_user(admin_engine)
    product_a = await _product(admin_engine, org_a, actor_a)
    product_b = await _product(admin_engine, org_b, actor_b)

    licenses: dict[str, UUID] = {}
    for key, org_id, product_id, actor_id in (
        ("a", org_a, product_a, actor_a),
        ("b", org_b, product_b, actor_b),
    ):
        row = License(
            org_id=org_id,
            product_id=product_id,
            vendor_id=None,
            type="perpetual",
            entitlements=1,
            key_sealed=None,
            notes="",
            created_by=actor_id,
        )
        bypassrls_session.add(row)
        await bypassrls_session.flush()
        licenses[key] = row.id

    assert await get_license(bypassrls_session, org_a, licenses["b"]) is None, (
        "get_license answered with org B's row on a session where RLS is not filtering"
    )
    assert await get_license(bypassrls_session, org_a, licenses["a"]) is not None, (
        "...and the fixture really did have a row of its own to find"
    )

    assert [lic.id for lic in await list_licenses(bypassrls_session, org_a)] == [licenses["a"]]
    assert [lic.id for lic in await list_licenses(bypassrls_session, org_b)] == [licenses["b"]]

    # The write path resolves through `_load_license`, so the destructive
    # direction is asserted too: a miss here deletes a neighbour's row.
    with pytest.raises(LicenseNotFoundError):
        await delete_license(
            bypassrls_session, org_id=org_a, actor_id=actor_a, license_id=licenses["b"]
        )
    still_there = await get_license(bypassrls_session, org_b, licenses["b"])
    assert still_there is not None, "org A's delete reached across the tenant boundary"
