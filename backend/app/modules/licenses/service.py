"""Licenses/SAM vendor service (docs/modules/licenses/spec.md): CRUD for the
vendor entity shared by the module's financial trio (product/license/
allocation).

Mirrors `app.modules.bcp.service`'s org-scoped-load + audit + flush style:
every query in this module carries an explicit `org_id` predicate (invariant
#5) so a vendor id from another org is never reachable through any function
here, and every mutation is followed by `audit_service.record` + a flush
(invariant #4).

This is the first function set in this file; later tasks append
product/license/allocation/reconciliation functions alongside these.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit_service
from app.core.i18n import tr
from app.core.vault import VaultService
from app.core.wire_words import say
from app.models import (
    Device,
    InstalledSoftware,
    License,
    LicenseAllocation,
    Person,
    SoftwareIgnore,
    SoftwareProduct,
    User,
    Vendor,
)
from app.modules.licenses.errors import LicenseNotFoundError, LicenseValidationError
from app.modules.licenses.matching import rule_matches, validate_match_rules
from app.modules.licenses.words import ALLOCATION_TARGET_WORDS
from app.modules.people.service import person_id_for_user

# Fields `update_vendor` is allowed to apply. Only non-None values passed by
# the caller are actually written.
_VENDOR_EDITABLE_FIELDS = ("name", "website", "contacts")

# Fields `update_product` is allowed to apply. Only non-None values passed by
# the caller are actually written.
_PRODUCT_EDITABLE_FIELDS = ("name", "publisher", "match_rules")

# Fields `update_license` is allowed to apply as plain scalar assignment.
# `key`, `product_id`, and `vendor_id` are handled separately above this
# list (key needs sealing; product_id/vendor_id need org-scoped
# re-validation) even though product_id/vendor_id are listed here too, so
# they still get applied via the same loop once validated.
_LICENSE_EDITABLE_FIELDS = (
    "type",
    "entitlements",
    "cost",
    "currency",
    "po_ref",
    "purchased_at",
    "maintenance_expires_at",
    "renews_at",
    "agreement_ref",
    "notes",
    "product_id",
    "vendor_id",
)


async def _load_vendor(session: AsyncSession, org_id: UUID, vendor_id: UUID) -> Vendor:
    vendor = (
        await session.execute(
            select(Vendor).where(Vendor.org_id == org_id, Vendor.id == vendor_id)
        )
    ).scalar_one_or_none()
    if vendor is None:
        raise LicenseNotFoundError(tr("vendor {vendor_id} not found", vendor_id=vendor_id))
    return vendor


async def create_vendor(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    name: str,
    website: str | None,
    contacts: list[dict[str, Any]],
) -> Vendor:
    """Create a new vendor and audit `license.vendor_created`."""
    vendor = Vendor(
        org_id=org_id,
        name=name,
        website=website,
        contacts=contacts,
        created_by=actor_id,
    )
    session.add(vendor)
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="license.vendor_created",
        target_type="vendor",
        target_id=str(vendor.id),
        metadata={"name": name},
    )
    await session.flush()
    return vendor


async def update_vendor(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    vendor_id: UUID,
    **fields: Any,
) -> Vendor:
    """Apply the given non-None fields to a vendor, org-scoped-loaded."""
    vendor = await _load_vendor(session, org_id, vendor_id)

    for field in _VENDOR_EDITABLE_FIELDS:
        if field not in fields:
            continue
        value = fields[field]
        if value is None:
            continue
        setattr(vendor, field, value)

    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="license.vendor_updated",
        target_type="vendor",
        target_id=str(vendor.id),
        metadata={},
    )
    await session.flush()
    return vendor


async def get_vendor(session: AsyncSession, org_id: UUID, vendor_id: UUID) -> Vendor | None:
    return (
        await session.execute(
            select(Vendor).where(Vendor.org_id == org_id, Vendor.id == vendor_id)
        )
    ).scalar_one_or_none()


async def list_vendors(session: AsyncSession, org_id: UUID) -> list[Vendor]:
    return list(
        (await session.execute(select(Vendor).where(Vendor.org_id == org_id)))
        .scalars()
        .all()
    )


# ---------------------------------------------------------------------------
# software_product (docs/modules/licenses/spec.md §4): the entity the
# matching engine (app.modules.licenses.matching) resolves installs against.
# ---------------------------------------------------------------------------


async def _load_product(
    session: AsyncSession, org_id: UUID, product_id: UUID
) -> SoftwareProduct:
    product = (
        await session.execute(
            select(SoftwareProduct).where(
                SoftwareProduct.org_id == org_id, SoftwareProduct.id == product_id
            )
        )
    ).scalar_one_or_none()
    if product is None:
        raise LicenseNotFoundError(tr("product {product_id} not found", product_id=product_id))
    return product


async def create_product(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    name: str,
    publisher: str,
    match_rules: list[dict[str, Any]],
) -> SoftwareProduct:
    """Create a new software product and audit `license.product_created`.

    `match_rules` is validated (`validate_match_rules`) before the product is
    persisted, so an invalid rule never enters the matching engine's input.
    """
    validate_match_rules(match_rules)

    product = SoftwareProduct(
        org_id=org_id,
        name=name,
        publisher=publisher,
        match_rules=match_rules,
        created_by=actor_id,
    )
    session.add(product)
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="license.product_created",
        target_type="product",
        target_id=str(product.id),
        metadata={"name": name},
    )
    await session.flush()
    return product


async def update_product(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    product_id: UUID,
    **fields: Any,
) -> SoftwareProduct:
    """Apply the given non-None fields to a product, org-scoped-loaded.

    If `match_rules` is among the supplied fields (and not None), it is
    validated (`validate_match_rules`) before being applied.
    """
    product = await _load_product(session, org_id, product_id)

    match_rules = fields.get("match_rules")
    if match_rules is not None:
        validate_match_rules(match_rules)

    for field in _PRODUCT_EDITABLE_FIELDS:
        if field not in fields:
            continue
        value = fields[field]
        if value is None:
            continue
        setattr(product, field, value)

    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="license.product_updated",
        target_type="product",
        target_id=str(product.id),
        metadata={},
    )
    await session.flush()
    return product


async def get_product(
    session: AsyncSession, org_id: UUID, product_id: UUID
) -> SoftwareProduct | None:
    return (
        await session.execute(
            select(SoftwareProduct).where(
                SoftwareProduct.org_id == org_id, SoftwareProduct.id == product_id
            )
        )
    ).scalar_one_or_none()


async def list_products(session: AsyncSession, org_id: UUID) -> list[SoftwareProduct]:
    return list(
        (
            await session.execute(
                select(SoftwareProduct).where(SoftwareProduct.org_id == org_id)
            )
        )
        .scalars()
        .all()
    )


# ---------------------------------------------------------------------------
# license (docs/modules/licenses/spec.md §5): vault-sealed keys, audited
# reveal. The license's own id is used as the vault `key_id` so a sealed key
# is cryptographically bound (via AAD) to that license and can never be
# relocated to another row -- this is why creation must flush once (to
# obtain `license.id`) before sealing.
# ---------------------------------------------------------------------------


async def _load_license(session: AsyncSession, org_id: UUID, license_id: UUID) -> License:
    license_ = (
        await session.execute(
            select(License).where(License.org_id == org_id, License.id == license_id)
        )
    ).scalar_one_or_none()
    if license_ is None:
        raise LicenseNotFoundError(tr("license {license_id} not found", license_id=license_id))
    return license_


async def _validate_product_ref(
    session: AsyncSession, org_id: UUID, product_id: UUID
) -> None:
    """Resolve `product_id` org-scoped; a cross-org/unknown reference from a
    request body is a validation error (400), not a not-found (404) --
    matching `app.modules.bcp.service`'s scope-ref convention."""
    try:
        await _load_product(session, org_id, product_id)
    except LicenseNotFoundError as exc:
        raise LicenseValidationError(
            tr("unknown product_id {product_id}", product_id=product_id)
        ) from exc


async def _validate_vendor_ref(
    session: AsyncSession, org_id: UUID, vendor_id: UUID
) -> None:
    try:
        await _load_vendor(session, org_id, vendor_id)
    except LicenseNotFoundError as exc:
        raise LicenseValidationError(
            tr("unknown vendor_id {vendor_id}", vendor_id=vendor_id)
        ) from exc


async def create_license(
    session: AsyncSession,
    vault: VaultService,
    *,
    org_id: UUID,
    actor_id: UUID,
    product_id: UUID,
    vendor_id: UUID | None,
    type_: str,
    entitlements: int,
    key: str | None,
    cost: Decimal | None,
    currency: str | None,
    po_ref: str | None,
    purchased_at: date | None,
    maintenance_expires_at: date | None,
    renews_at: date | None,
    agreement_ref: str | None,
    notes: str,
) -> License:
    """Create a new license. `product_id` (and `vendor_id`, if given) are
    resolved org-scoped first (invariant #5). If `key` is a non-empty
    string, it is vault-sealed under the license's own id (invariant #3) --
    the plaintext key is never persisted, never logged, and never included
    in the audit metadata."""
    await _validate_product_ref(session, org_id, product_id)
    if vendor_id is not None:
        await _validate_vendor_ref(session, org_id, vendor_id)

    license_ = License(
        org_id=org_id,
        product_id=product_id,
        vendor_id=vendor_id,
        type=type_,
        entitlements=entitlements,
        cost=cost,
        currency=currency,
        po_ref=po_ref,
        purchased_at=purchased_at,
        maintenance_expires_at=maintenance_expires_at,
        renews_at=renews_at,
        agreement_ref=agreement_ref,
        notes=notes,
        created_by=actor_id,
    )
    session.add(license_)
    await session.flush()  # populates license_.id (uuid7_pk) before sealing

    if key:
        license_.key_sealed = await vault.seal_bytes(
            session, org_id=org_id, key_id=license_.id, plaintext=key.encode()
        )
        await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="license.created",
        target_type="license",
        target_id=str(license_.id),
        metadata={"product_id": str(product_id), "entitlements": entitlements},
    )
    await session.flush()
    return license_


async def update_license(
    session: AsyncSession,
    vault: VaultService,
    *,
    org_id: UUID,
    actor_id: UUID,
    license_id: UUID,
    **fields: Any,
) -> License:
    """Apply the given non-None fields to a license, org-scoped-loaded.

    `key` is handled outside the plain scalar allow-list: an empty string
    clears the key (`key_sealed = None`); a non-empty string re-seals it
    (under the same `key_id=license.id` so the AAD binding stays valid);
    `key` absent or explicitly `None` leaves the existing sealed key
    untouched. `product_id`/`vendor_id`, if supplied and not None, are
    re-validated org-scoped before being applied.
    """
    license_ = await _load_license(session, org_id, license_id)

    product_id = fields.get("product_id")
    if product_id is not None:
        await _validate_product_ref(session, org_id, product_id)

    vendor_id = fields.get("vendor_id")
    if vendor_id is not None:
        await _validate_vendor_ref(session, org_id, vendor_id)

    for field in _LICENSE_EDITABLE_FIELDS:
        if field not in fields:
            continue
        value = fields[field]
        if value is None:
            continue
        setattr(license_, field, value)

    if "key" in fields:
        key = fields["key"]
        if key == "":
            license_.key_sealed = None
        elif key:
            license_.key_sealed = await vault.seal_bytes(
                session, org_id=org_id, key_id=license_.id, plaintext=key.encode()
            )
        # key is None (explicitly passed) -- leave the existing key untouched.

    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="license.updated",
        target_type="license",
        target_id=str(license_.id),
        metadata={},
    )
    await session.flush()
    return license_


async def delete_license(
    session: AsyncSession, *, org_id: UUID, actor_id: UUID, license_id: UUID
) -> None:
    license_ = await _load_license(session, org_id, license_id)
    license_id = license_.id
    await session.delete(license_)
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="license.deleted",
        target_type="license",
        target_id=str(license_id),
        metadata={},
    )
    await session.flush()


async def get_license(
    session: AsyncSession, org_id: UUID, license_id: UUID
) -> License | None:
    return (
        await session.execute(
            select(License).where(License.org_id == org_id, License.id == license_id)
        )
    ).scalar_one_or_none()


async def list_licenses(session: AsyncSession, org_id: UUID) -> list[License]:
    return list(
        (await session.execute(select(License).where(License.org_id == org_id)))
        .scalars()
        .all()
    )


async def reveal_key(
    session: AsyncSession,
    vault: VaultService,
    *,
    org_id: UUID,
    actor_id: UUID,
    license_id: UUID,
) -> str:
    """The ONLY function in this module that returns a license key in
    plaintext. Audited (invariant #4): every call that returns a key writes
    a `license.key_revealed` event, whether or not the caller subsequently
    does anything with the value."""
    license_ = await _load_license(session, org_id, license_id)
    if license_.key_sealed is None:
        raise LicenseValidationError(tr("license has no key"))

    plaintext = (
        await vault.open_bytes(
            session, org_id=org_id, key_id=license_.id, sealed=license_.key_sealed
        )
    ).decode()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="license.key_revealed",
        target_type="license",
        target_id=str(license_.id),
        metadata={},
    )
    await session.flush()
    return plaintext


# ---------------------------------------------------------------------------
# allocation (docs/modules/licenses/spec.md §6): assigning seats to devices/
# users, enforcing the license's entitlement pool. The target (`device` or
# `user`) is resolved org-scoped before an allocation row is created, so a
# cross-org or unknown target id can never be recorded against a license
# (invariant #5) -- an explicit `org_id` predicate on the target table is the
# sole guard, not RLS alone (RLS is a backstop, not the only check).
# ---------------------------------------------------------------------------


_TARGET_TYPE_TO_MODEL: dict[str, type] = {"device": Device, "user": User, "person": Person}


async def _validate_allocation_target(
    session: AsyncSession, org_id: UUID, target_type: str, target_id: UUID
) -> None:
    """Resolve an allocation target org-scoped, whitelisting `target_type`.

    An unknown `target_type` (anything other than "device"/"user"/"person") raises
    rather than silently falling through to a `User` lookup -- mirrors
    `app.modules.bcp.service._validate_scope`'s kind whitelist. A cross-org or
    unknown `target_id` is indistinguishable from nonexistent and both raise
    (invariant #5). The API layer's `Literal` already blocks a bad
    `target_type` over HTTP; this keeps the service invariant true for any
    caller (defense in depth).
    """
    model = _TARGET_TYPE_TO_MODEL.get(target_type)
    if model is None:
        raise LicenseValidationError(
            tr("unknown target_type {target_type}", target_type=repr(target_type))
        )
    found = (
        await session.execute(
            select(model.id).where(model.org_id == org_id, model.id == target_id)  # type: ignore[attr-defined]
        )
    ).scalar_one_or_none()
    if found is None:
        raise LicenseValidationError(
            tr(
                "{target_type} target not found: {target_id}",
                target_type=say(ALLOCATION_TARGET_WORDS, target_type),
                target_id=target_id,
            )
        )


async def _active_allocation_count(
    session: AsyncSession, org_id: UUID, license_id: UUID
) -> int:
    return (
        await session.execute(
            select(func.count(LicenseAllocation.id)).where(
                LicenseAllocation.org_id == org_id,
                LicenseAllocation.license_id == license_id,
                LicenseAllocation.released_at.is_(None),
            )
        )
    ).scalar_one()


async def allocate(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    license_id: UUID,
    target_type: str,
    target_id: UUID,
) -> LicenseAllocation:
    """Assign a seat of `license_id` to a device or user, enforcing the pool.

    `license_id` and the target are both resolved org-scoped first
    (invariant #5): a cross-org or unknown target is indistinguishable and
    both raise `LicenseValidationError`. If the license's active-allocation
    count has already reached `entitlements`, the pool is exhausted and no
    row is created.

    The capacity check below is NOT protected by any unique index -- unlike
    a duplicate-key check, "how many active allocations exist" has nothing
    for Postgres to arbitrate a conflict on, so `ON CONFLICT DO NOTHING`
    would do nothing to prevent an overfill. Two concurrent callers can
    both read `active < license_.entitlements` as true before either has
    inserted its `LicenseAllocation`, and both would then insert,
    overfilling the pool by one. The fix is a `SELECT ... FOR UPDATE` on
    the license row itself: it takes a row lock for the rest of THIS
    transaction, so a second concurrent `allocate` call for the same
    `license_id` blocks at this line until the first commits or rolls back,
    and then re-runs the count against the first call's now-visible row (or
    its absence, if the first rolled back)."""
    license_ = await _load_license(session, org_id, license_id)
    await _validate_allocation_target(session, org_id, target_type, target_id)
    await session.execute(
        select(License.id)
        .where(License.org_id == org_id, License.id == license_id)
        .with_for_update()
    )

    active = await _active_allocation_count(session, org_id, license_id)
    if active >= license_.entitlements:
        raise LicenseValidationError(tr("license pool exhausted"))

    # The person who holds it (docs/modules/people/spec.md §0.4); none for a device.
    person_id: UUID | None = None
    if target_type == "person":
        person_id = target_id
    elif target_type == "user":
        person_id = await person_id_for_user(session, org_id, target_id)

    allocation = LicenseAllocation(
        org_id=org_id,
        license_id=license_id,
        target_type=target_type,
        target_id=target_id,
        person_id=person_id,
        created_by=actor_id,
    )
    session.add(allocation)
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="license.allocated",
        target_type="license",
        target_id=str(license_id),
        metadata={"target_type": target_type, "target_id": str(target_id)},
    )
    await session.flush()
    return allocation


async def _load_allocation(
    session: AsyncSession, org_id: UUID, allocation_id: UUID
) -> LicenseAllocation:
    allocation = (
        await session.execute(
            select(LicenseAllocation).where(
                LicenseAllocation.org_id == org_id, LicenseAllocation.id == allocation_id
            )
        )
    ).scalar_one_or_none()
    if allocation is None:
        raise LicenseNotFoundError(
            tr("allocation {allocation_id} not found", allocation_id=allocation_id)
        )
    return allocation


async def release_allocation(
    session: AsyncSession, *, org_id: UUID, actor_id: UUID, allocation_id: UUID
) -> LicenseAllocation:
    """Release a previously granted seat, freeing a slot in the pool.

    Releasing an already-released allocation is a validation error, not a
    silent no-op -- it usually indicates a double-submit or a stale UI
    state the caller should surface."""
    allocation = await _load_allocation(session, org_id, allocation_id)
    if allocation.released_at is not None:
        raise LicenseValidationError(tr("allocation already released"))

    allocation.released_at = datetime.now(UTC)
    await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="license.allocation_released",
        target_type="license_allocation",
        target_id=str(allocation_id),
        metadata={},
    )
    await session.flush()
    return allocation


async def list_allocations(
    session: AsyncSession, org_id: UUID, license_id: UUID
) -> list[LicenseAllocation]:
    return list(
        (
            await session.execute(
                select(LicenseAllocation).where(
                    LicenseAllocation.org_id == org_id,
                    LicenseAllocation.license_id == license_id,
                )
            )
        )
        .scalars()
        .all()
    )


async def available_pool(session: AsyncSession, org_id: UUID, license: License) -> int:
    active = await _active_allocation_count(session, org_id, license.id)
    return license.entitlements - active


# ---------------------------------------------------------------------------
# reconciliation (docs/modules/licenses/spec.md §4, review queue): the
# operator-facing surface for installed-software that matches no product's
# rules -- `review_queue` surfaces it (read-only, no audit); `map_software`
# and `ignore_software` are the two ways an operator resolves an entry so it
# stops reappearing.
# ---------------------------------------------------------------------------


async def review_queue(session: AsyncSession, org_id: UUID) -> list[dict[str, Any]]:
    """The org's distinct installed-software `(name, publisher)` that match NO
    product's rules AND are not in `software_ignore`, each with `device_count`
    (distinct devices running that name), ordered by `device_count` desc
    (stable tiebreak by name). Read-only -- no audit.
    """
    products = list(
        (
            await session.execute(
                select(SoftwareProduct).where(SoftwareProduct.org_id == org_id)
            )
        )
        .scalars()
        .all()
    )

    installs = (
        await session.execute(
            select(
                InstalledSoftware.device_id,
                InstalledSoftware.name,
                InstalledSoftware.publisher,
            ).where(InstalledSoftware.org_id == org_id)
        )
    ).all()

    ignored_names = {
        name.casefold()
        for name in (
            await session.execute(
                select(SoftwareIgnore.name).where(SoftwareIgnore.org_id == org_id)
            )
        )
        .scalars()
        .all()
    }

    # distinct (name, publisher) -> set of device_ids running it
    grouped: dict[tuple[str, str], set[UUID]] = {}
    for device_id, name, publisher in installs:
        grouped.setdefault((name, publisher), set()).add(device_id)

    unmatched: list[dict[str, Any]] = []
    for (name, publisher), device_ids in grouped.items():
        if name.casefold() in ignored_names:
            continue
        if any(
            rule_matches(rule, name, publisher)
            for product in products
            for rule in product.match_rules
        ):
            continue
        unmatched.append(
            {"name": name, "publisher": publisher, "device_count": len(device_ids)}
        )

    unmatched.sort(key=lambda row: (-row["device_count"], row["name"]))
    return unmatched


async def map_software(
    session: AsyncSession,
    *,
    org_id: UUID,
    actor_id: UUID,
    name: str,
    product_id: UUID,
) -> SoftwareProduct:
    """Resolve a review-queue entry by mapping it onto `product_id`: appends
    an `exact` match rule for `name` to that product's `match_rules`
    (org-scoped product resolve -- a cross-org/missing `product_id` is a
    `LicenseNotFoundError`). Dedupe-safe: an identical rule is not appended
    twice. The JSONB list is reassigned rather than mutated in place so
    SQLAlchemy detects the change."""
    product = await _load_product(session, org_id, product_id)

    new_rule = {"type": "exact", "pattern": name, "publisher": None}
    if new_rule not in product.match_rules:
        product.match_rules = [*product.match_rules, new_rule]
        await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="license.software_mapped",
        target_type="product",
        target_id=str(product_id),
        metadata={"name": name},
    )
    await session.flush()
    return product


async def ignore_software(
    session: AsyncSession, *, org_id: UUID, actor_id: UUID, name: str
) -> SoftwareIgnore:
    """Resolve a review-queue entry by ignoring it: inserts a `software_ignore`
    row. Idempotent on the unique key -- if a row already exists for this name
    in the row's namespace, it is returned as-is without inserting a duplicate.

    The row is GLOBAL (phase 1 writes no client), so its namespace is the
    global rows': `uq_software_ignore_client_name` is per client (migration
    0231, Task 8b), and a client's ignore of the same name is another row,
    never this one."""
    existing = (
        await session.execute(
            select(SoftwareIgnore).where(
                SoftwareIgnore.org_id == org_id,
                SoftwareIgnore.client_id.is_(None),
                SoftwareIgnore.name == name,
            )
        )
    ).scalar_one_or_none()

    if existing is not None:
        ignore = existing
    else:
        ignore = SoftwareIgnore(org_id=org_id, name=name, created_by=actor_id)
        session.add(ignore)
        await session.flush()

    await audit_service.record(
        session,
        org_id=org_id,
        actor_type="user",
        actor_id=actor_id,
        action="license.software_ignored",
        target_type="software_ignore",
        target_id=str(ignore.id),
        metadata={"name": name},
    )
    await session.flush()
    return ignore
