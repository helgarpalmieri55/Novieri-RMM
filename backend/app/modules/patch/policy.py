"""Device-group patch-policy resolution (docs/modules/patch/spec.md).

Resolution order: device's group policy -> org-default policy
(device_group_id IS NULL) -> hardcoded safe defaults.

The org-default lookup is deliberately not `.scalar_one_or_none()`-on-a-plain-
select: Postgres's UNIQUE(org_id, device_group_id) constraint does not prevent
multiple rows with a NULL device_group_id (NULLs are never considered equal
for uniqueness purposes), so more than one org-default row can exist for the
same org. We order deterministically and take the first row rather than
raising when duplicates exist.
"""

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models import Device, PatchPolicy


@dataclass(frozen=True)
class ResolvedPatchPolicy:
    ring_name: str = "broad"
    ring_order: int = 100
    maintenance_window: str | None = None
    auto_approve_security: bool = False
    reboot: str = "if_required"
    reboot_deferral_hours: int = 24
    scan_interval_hours: int = 24


def _from_row(row: PatchPolicy) -> ResolvedPatchPolicy:
    return ResolvedPatchPolicy(
        ring_name=row.ring_name,
        ring_order=row.ring_order,
        maintenance_window=row.maintenance_window,
        auto_approve_security=row.auto_approve_security,
        reboot=row.reboot,
        reboot_deferral_hours=row.reboot_deferral_hours,
        scan_interval_hours=row.scan_interval_hours,
    )


async def resolve_patch_policy(
    session: AsyncSession, org_id: UUID, device_id: UUID
) -> ResolvedPatchPolicy:
    group_id = (
        await session.execute(
            select(Device.device_group_id).where(
                Device.id == device_id, Device.org_id == org_id
            )
        )
    ).scalar_one_or_none()
    if group_id is not None:
        row = (
            await session.execute(
                select(PatchPolicy).where(
                    PatchPolicy.org_id == org_id, PatchPolicy.device_group_id == group_id
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            return _from_row(row)
    org_default = (
        await session.execute(
            select(PatchPolicy)
            .where(PatchPolicy.org_id == org_id, PatchPolicy.device_group_id.is_(None))
            .order_by(PatchPolicy.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    return _from_row(org_default) if org_default is not None else ResolvedPatchPolicy()


@dataclass(frozen=True)
class RingPopulation:
    """One deployment ring and every device of this org that resolves into it.

    `ring_order` is the rollout position the ring's policies declare -- the
    LOWEST, when several policies name the same ring, because a ring is as early
    in the rollout as its earliest declaration.
    """

    ring_name: str
    ring_order: int
    device_ids: tuple[UUID, ...]
    #: How many of `device_ids` are each client's: the reach of a client's
    #: approval into this ring, where a global one reaches all of them (MSP
    #: clients plan 2026-10-26 Task 14 review). Derived from `device_ids`, so
    #: it takes no part in equality.
    client_counts: Mapping[UUID, int] = field(default_factory=dict, compare=False)


async def resolve_ring_populations(
    session: AsyncSession, org_id: UUID, *, client_id: UUID | None = None
) -> list[RingPopulation]:
    """Every ring in this org, in rollout order, with the devices in each --
    `client_id`'s devices only, when given: the population a client's patch
    approval reaches (MSP clients plan 2026-10-26 Task 14). The rings
    themselves are the org's either way.

    THE SAME QUESTION `resolve_patch_policy` ANSWERS, ASKED ONCE. Resolution
    order is identical and `tests/test_patch_policy.py::
    test_ring_populations_agree_with_the_per_device_resolver` pins that device by
    device: a group policy wins, else the org default (`device_group_id IS
    NULL`, lowest id, because Postgres's UNIQUE permits several NULL rows), else
    `ResolvedPatchPolicy()`'s hardcoded `broad`.

    WHY IT IS NOT A LOOP OVER `resolve_patch_policy`. Approval flips the status
    rows of every device in the approved ring, so `approve.py` needed the ring's
    population -- and resolved it PER DEVICE, PER PATCH. Approving four hundred
    catalogued updates across a two-hundred-machine fleet was eighty thousand
    resolutions of an answer that does not change inside a batch. The join below
    is one statement; the batch resolves once and reuses it.

    THE JOIN REPRODUCES THE FALLBACK RATHER THAN RESTATING IT. `pp.device_group_id
    = d.device_group_id` never matches NULL to NULL in SQL, so an ungrouped
    device and a grouped device whose group names no policy both arrive with a
    NULL ring and take the org default -- which is exactly what the per-device
    resolver does, and is why the org-default row (whose `device_group_id` IS
    NULL) can never accidentally join to an ungrouped machine.

    A RING NAMED BY A POLICY IS LISTED EVEN WITH NOBODY IN IT. A ring an operator
    can approve into and cannot see on the page is a ring they will approve into
    by accident; zero is the honest number, and it also keeps the rings a
    PARTITION of the fleet, which is what makes the counts addable.
    """
    org_default = (
        await session.execute(
            select(PatchPolicy)
            .where(PatchPolicy.org_id == org_id, PatchPolicy.device_group_id.is_(None))
            .order_by(PatchPolicy.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    fallback = (
        _from_row(org_default) if org_default is not None else ResolvedPatchPolicy()
    )

    orders: dict[str, int] = {fallback.ring_name: fallback.ring_order}
    members: dict[str, list[UUID]] = {fallback.ring_name: []}
    clients: dict[str, Counter[UUID]] = {fallback.ring_name: Counter()}

    named = (
        await session.execute(
            select(PatchPolicy.ring_name, PatchPolicy.ring_order).where(
                PatchPolicy.org_id == org_id
            )
        )
    ).all()
    for ring_name, ring_order in named:
        orders[ring_name] = min(orders.get(ring_name, ring_order), ring_order)
        members.setdefault(ring_name, [])
        clients.setdefault(ring_name, Counter())

    group_policy = aliased(PatchPolicy)
    rows = (
        await session.execute(
            select(Device.id, Device.client_id, group_policy.ring_name)
            .select_from(Device)
            .outerjoin(
                group_policy,
                and_(
                    group_policy.org_id == org_id,
                    group_policy.device_group_id == Device.device_group_id,
                ),
            )
            .where(
                Device.org_id == org_id,
                *([] if client_id is None else [Device.client_id == client_id]),
            )
            .order_by(Device.id)
        )
    ).all()
    for device_id, device_client, ring_name in rows:
        members[ring_name or fallback.ring_name].append(device_id)
        clients[ring_name or fallback.ring_name][device_client] += 1

    return [
        RingPopulation(
            ring_name=ring_name,
            ring_order=orders[ring_name],
            device_ids=tuple(device_ids),
            client_counts=dict(clients[ring_name]),
        )
        for ring_name, device_ids in sorted(
            members.items(), key=lambda item: (orders[item[0]], item[0])
        )
    ]
