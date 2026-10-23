"""Tests for the Licenses/SAM allocation service (docs/modules/licenses/spec.md
§6): assigning seats to devices/users with pool enforcement.

Mirrors `tests/test_licenses_vendor.py`'s / `tests/test_licenses_service.py`'s
fixture style: a throwaway org + user seeded via `admin_engine`, service
calls run through an RLS-constrained `org_session`, audit rows read back by
`target_type`/`target_id`.
"""

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.core.client_scope import ClientScope
from app.core.tenancy import org_session
from app.core.vault import LocalVaultService
from app.models import AuditEvent, Device, License, Organization, User
from app.modules.licenses.errors import LicenseNotFoundError, LicenseValidationError
from app.modules.licenses.service import (
    allocate,
    available_pool,
    create_license,
    create_product,
    list_allocations,
    release_allocation,
)


async def _org_with_user(admin_engine: AsyncEngine) -> tuple[UUID, UUID]:
    slug = f"licalloc-{uuid4().hex[:6]}"
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


def _vault(tmp_path: Path) -> LocalVaultService:
    return LocalVaultService(tmp_path / "root.key")


async def _license(
    app_engine: AsyncEngine,
    org_id: UUID,
    actor_id: UUID,
    entitlements: int,
    vault: LocalVaultService,
) -> UUID:
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        product = await create_product(
            s,
            org_id=org_id,
            actor_id=actor_id,
            name="Product",
            publisher="Pub",
            match_rules=[],
        )
        license_ = await create_license(
            s,
            vault,
            org_id=org_id,
            actor_id=actor_id,
            product_id=product.id,
            vendor_id=None,
            type_="volume",
            entitlements=entitlements,
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
        return license_.id


async def _audit_actions(
    app_engine: AsyncEngine, org_id: UUID, target_type: str, target_id: str
) -> list[str]:
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        events = (
            await s.execute(
                select(AuditEvent)
                .where(
                    AuditEvent.org_id == org_id,
                    AuditEvent.target_type == target_type,
                    AuditEvent.target_id == target_id,
                )
                .order_by(AuditEvent.id)
            )
        ).scalars().all()
        return [e.action for e in events]


# ---------------------------------------------------------------------------
# allocate / pool enforcement
# ---------------------------------------------------------------------------


async def test_allocate_fills_pool_then_exhausts(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    device_id = await _seed_device(admin_engine, org_id, "device-a")
    vault = _vault(tmp_path)
    license_id = await _license(app_engine, org_id, actor_id, entitlements=2, vault=vault)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        alloc1 = await allocate(
            s,
            org_id=org_id,
            actor_id=actor_id,
            license_id=license_id,
            target_type="device",
            target_id=device_id,
        )
        await s.commit()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        alloc2 = await allocate(
            s,
            org_id=org_id,
            actor_id=actor_id,
            license_id=license_id,
            target_type="user",
            target_id=actor_id,
        )
        await s.commit()

    assert alloc1.target_type == "device"
    assert alloc1.target_id == device_id
    assert alloc2.target_type == "user"
    assert alloc2.target_id == actor_id
    assert alloc1.released_at is None
    assert alloc2.released_at is None

    other_device_id = await _seed_device(admin_engine, org_id, "device-b")
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(LicenseValidationError, match="license pool exhausted"):
            await allocate(
                s,
                org_id=org_id,
                actor_id=actor_id,
                license_id=license_id,
                target_type="device",
                target_id=other_device_id,
            )

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        allocations = await list_allocations(s, org_id, license_id)
        assert len(allocations) == 2


async def test_allocate_concurrent_calls_cannot_exceed_pool(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """`allocate`'s capacity check (`active >= license_.entitlements`) is
    NOT backed by any unique index -- a license pool's SIZE is not a
    uniqueness fact, so `ON CONFLICT DO NOTHING` (the fix for a true
    duplicate-key race) would do nothing here: two concurrent callers can
    both read `active < entitlements` as true before either has inserted
    its `LicenseAllocation`, and both would then insert, overfilling the
    pool by one.

    The fix is a `SELECT ... FOR UPDATE` on the license row, taken before
    the count is read (see `allocate`'s docstring): it serializes
    concurrent callers for the SAME license, so a second call blocks until
    the first commits (or rolls back), and only then re-reads the count.

    Mirrors `test_subscriptions_seats.py`'s
    `test_assign_seat_concurrent_calls_cannot_exceed_pool`: a bare
    `asyncio.gather` of two attempts does not reliably exercise this
    (connection acquisition staggers the two calls enough that each one's
    SELECT already sees the other's committed row). Proving the row lock
    actually blocks needs a deterministic technique: session A takes the
    single entitlement and DELIBERATELY keeps its transaction open;
    session B's `allocate` is started concurrently as a real background
    task on its own connection, and is asserted to still be unfinished
    after a generous timeout -- i.e. genuinely blocked on A's row lock, not
    just slow. Only once that is proven does A commit, unblocking B, which
    must then see the entitlement already taken and raise the typed error
    rather than a raw `IntegrityError`."""
    org_id, actor_id = await _org_with_user(admin_engine)
    device_a = await _seed_device(admin_engine, org_id, "device-a")
    device_b = await _seed_device(admin_engine, org_id, "device-b")
    vault = _vault(tmp_path)
    license_id = await _license(app_engine, org_id, actor_id, entitlements=1, vault=vault)

    outcome: dict[str, str | None] = {}

    async def attempt_b() -> None:
        async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session_b:
            try:
                await allocate(
                    session_b,
                    org_id=org_id,
                    actor_id=actor_id,
                    license_id=license_id,
                    target_type="device",
                    target_id=device_b,
                )
            except LicenseValidationError as exc:
                outcome["error"] = str(exc)
            else:
                outcome["error"] = None
                await session_b.commit()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as session_a:
        # Session A takes the pool's only entitlement. Its transaction (and
        # the `FOR UPDATE` lock it holds on the license row) stays open
        # until the explicit commit below.
        await allocate(
            session_a,
            org_id=org_id,
            actor_id=actor_id,
            license_id=license_id,
            target_type="device",
            target_id=device_a,
        )

        task_b = asyncio.create_task(attempt_b())

        # Prove B is genuinely blocked on the row lock -- not merely slow --
        # by requiring it to still be unfinished after a generous timeout
        # while A's transaction is deliberately still open. `shield` stops
        # the timeout from cancelling B's task; it keeps running in the
        # background, still parked on Postgres's lock wait.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(task_b), timeout=1.0)
        assert not task_b.done()

        # Release the lock: B's blocked `SELECT ... FOR UPDATE` can now
        # proceed and will see Alice's committed allocation.
        await session_a.commit()

    await asyncio.wait_for(task_b, timeout=5.0)

    # B lost the race: the typed error, never a raw IntegrityError.
    assert outcome["error"] == "license pool exhausted"

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        allocations = await list_allocations(s, org_id, license_id)
        # The pool was never overfilled: exactly one allocation exists, not two.
        assert len(allocations) == 1
        assert allocations[0].target_id == device_a


async def test_available_pool_reflects_allocations_and_release(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    device_id = await _seed_device(admin_engine, org_id, "device-a")
    vault = _vault(tmp_path)
    license_id = await _license(app_engine, org_id, actor_id, entitlements=2, vault=vault)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        license_ = (
            await s.execute(select(License).where(License.id == license_id))
        ).scalar_one()
        pool = await available_pool(s, org_id, license_)
        assert pool == 2

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        alloc = await allocate(
            s,
            org_id=org_id,
            actor_id=actor_id,
            license_id=license_id,
            target_type="device",
            target_id=device_id,
        )
        await s.commit()
        alloc_id = alloc.id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        license_ = (
            await s.execute(select(License).where(License.id == license_id))
        ).scalar_one()
        pool = await available_pool(s, org_id, license_)
        assert pool == 1

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await allocate(
            s,
            org_id=org_id,
            actor_id=actor_id,
            license_id=license_id,
            target_type="user",
            target_id=actor_id,
        )
        await s.commit()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        license_ = (
            await s.execute(select(License).where(License.id == license_id))
        ).scalar_one()
        pool = await available_pool(s, org_id, license_)
        assert pool == 0

    # release frees a slot
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        released = await release_allocation(
            s, org_id=org_id, actor_id=actor_id, allocation_id=alloc_id
        )
        await s.commit()
        assert released.released_at is not None

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        license_ = (
            await s.execute(select(License).where(License.id == license_id))
        ).scalar_one()
        pool = await available_pool(s, org_id, license_)
        assert pool == 1

    # and a new allocate now succeeds again
    other_device_id = await _seed_device(admin_engine, org_id, "device-b")
    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        new_alloc = await allocate(
            s,
            org_id=org_id,
            actor_id=actor_id,
            license_id=license_id,
            target_type="device",
            target_id=other_device_id,
        )
        await s.commit()
        assert new_alloc.released_at is None


async def test_double_release_raises_validation_error(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    device_id = await _seed_device(admin_engine, org_id, "device-a")
    vault = _vault(tmp_path)
    license_id = await _license(app_engine, org_id, actor_id, entitlements=1, vault=vault)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        alloc = await allocate(
            s,
            org_id=org_id,
            actor_id=actor_id,
            license_id=license_id,
            target_type="device",
            target_id=device_id,
        )
        await s.commit()
        alloc_id = alloc.id

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await release_allocation(s, org_id=org_id, actor_id=actor_id, allocation_id=alloc_id)
        await s.commit()

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(LicenseValidationError, match="already released"):
            await release_allocation(
                s, org_id=org_id, actor_id=actor_id, allocation_id=alloc_id
            )


async def test_allocate_cross_org_target_raises_validation_error(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, _actor_b = await _org_with_user(admin_engine)
    foreign_device = await _seed_device(admin_engine, org_b, "foreign-host")
    vault = _vault(tmp_path)
    license_id = await _license(app_engine, org_a, actor_a, entitlements=5, vault=vault)

    async with org_session(org_a, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(LicenseValidationError):
            await allocate(
                s,
                org_id=org_a,
                actor_id=actor_a,
                license_id=license_id,
                target_type="device",
                target_id=foreign_device,
            )


async def test_allocate_target_resolve_org_filter_is_load_bearing(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Isolate the explicit `Device.org_id == org_id` predicate in the
    target-resolve query so it is the SOLE tenant guard.

    The test above also proves cross-org rejection, but there the
    RLS-constrained `org_session` is filtering rows too, so that assertion
    would still pass even if the explicit `org_id` predicate in the
    allocate() target-resolve query regressed. Here we run `allocate`
    directly on an RLS-bypassing `admin_engine` session (mirrors
    `tests/test_bcp_test_service.py`'s
    `test_evidence_source_queries_org_filter_is_load_bearing` pattern),
    removing RLS as a backstop. If the `Device.org_id == org_id` predicate
    in the target-resolve step were deleted, the foreign device would
    resolve and this test would fail (no exception raised).
    """
    org_a, actor_a = await _org_with_user(admin_engine)
    org_b, _actor_b = await _org_with_user(admin_engine)
    foreign_device = await _seed_device(admin_engine, org_b, "foreign-host")
    vault = _vault(tmp_path)
    license_id = await _license(app_engine, org_a, actor_a, entitlements=5, vault=vault)

    async with AsyncSession(admin_engine, expire_on_commit=False) as s:
        with pytest.raises(LicenseValidationError):
            await allocate(
                s,
                org_id=org_a,
                actor_id=actor_a,
                license_id=license_id,
                target_type="device",
                target_id=foreign_device,
            )


async def test_allocate_unknown_target_type_raises_validation_error(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    # A target_type outside the {device, user} whitelist must raise. This is
    # load-bearing: target_id is a REAL user id (actor_id), so the old
    # fall-through-to-User code would have FOUND it and silently allocated
    # under the bogus "printer" label — the whitelist is what rejects it.
    org_id, actor_id = await _org_with_user(admin_engine)
    vault = _vault(tmp_path)
    license_id = await _license(app_engine, org_id, actor_id, entitlements=5, vault=vault)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(LicenseValidationError):
            await allocate(
                s,
                org_id=org_id,
                actor_id=actor_id,
                license_id=license_id,
                target_type="printer",  # not in the {device, user} whitelist
                target_id=actor_id,  # a real user id → old code would resolve it
            )


async def test_allocate_unknown_target_raises_validation_error(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    vault = _vault(tmp_path)
    license_id = await _license(app_engine, org_id, actor_id, entitlements=5, vault=vault)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(LicenseValidationError):
            await allocate(
                s,
                org_id=org_id,
                actor_id=actor_id,
                license_id=license_id,
                target_type="device",
                target_id=uuid4(),
            )


# ---------------------------------------------------------------------------
# not-found paths
# ---------------------------------------------------------------------------


async def test_allocate_missing_license_raises_not_found(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    device_id = await _seed_device(admin_engine, org_id, "device-a")

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(LicenseNotFoundError):
            await allocate(
                s,
                org_id=org_id,
                actor_id=actor_id,
                license_id=uuid4(),
                target_type="device",
                target_id=device_id,
            )


async def test_release_allocation_missing_raises_not_found(
    admin_engine: AsyncEngine, app_engine: AsyncEngine
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        with pytest.raises(LicenseNotFoundError):
            await release_allocation(
                s, org_id=org_id, actor_id=actor_id, allocation_id=uuid4()
            )


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------


async def test_allocate_and_release_audit_actions(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    device_id = await _seed_device(admin_engine, org_id, "device-a")
    vault = _vault(tmp_path)
    license_id = await _license(app_engine, org_id, actor_id, entitlements=1, vault=vault)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        alloc = await allocate(
            s,
            org_id=org_id,
            actor_id=actor_id,
            license_id=license_id,
            target_type="device",
            target_id=device_id,
        )
        await s.commit()
        alloc_id = alloc.id

    actions = await _audit_actions(app_engine, org_id, "license", str(license_id))
    assert "license.allocated" in actions

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        event = (
            await s.execute(
                select(AuditEvent)
                .where(
                    AuditEvent.org_id == org_id, AuditEvent.action == "license.allocated"
                )
            )
        ).scalar_one()
        assert event.target_type == "license"
        assert event.target_id == str(license_id)
        assert event.meta == {"target_type": "device", "target_id": str(device_id)}

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await release_allocation(s, org_id=org_id, actor_id=actor_id, allocation_id=alloc_id)
        await s.commit()

    release_actions = await _audit_actions(
        app_engine, org_id, "license_allocation", str(alloc_id)
    )
    assert release_actions == ["license.allocation_released"]


async def test_list_allocations_and_available_pool_write_no_audit(
    admin_engine: AsyncEngine, app_engine: AsyncEngine, tmp_path: Path
) -> None:
    org_id, actor_id = await _org_with_user(admin_engine)
    device_id = await _seed_device(admin_engine, org_id, "device-a")
    vault = _vault(tmp_path)
    license_id = await _license(app_engine, org_id, actor_id, entitlements=2, vault=vault)

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await allocate(
            s,
            org_id=org_id,
            actor_id=actor_id,
            license_id=license_id,
            target_type="device",
            target_id=device_id,
        )
        await s.commit()

    before = await _audit_actions(app_engine, org_id, "license", str(license_id))

    async with org_session(org_id, engine=app_engine, clients=ClientScope.all()) as s:
        await list_allocations(s, org_id, license_id)
        license_ = (
            await s.execute(select(License).where(License.id == license_id))
        ).scalar_one()
        await available_pool(s, org_id, license_)

    after = await _audit_actions(app_engine, org_id, "license", str(license_id))
    assert after == before
