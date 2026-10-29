"""The Person page payload (docs/modules/people/spec.md §4).

A section the caller may not read in its owning module is `None`, never an
empty list: "hidden" and "has none" are different answers.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rbac import user_permission_codes, user_role_names
from app.models import (
    Device,
    DeviceGroup,
    DevicePerson,
    License,
    LicenseAllocation,
    Person,
    PersonChecklist,
    SeatAssignment,
    SoftwareProduct,
    Subscription,
    Ticket,
    User,
)
from app.modules.people.checklists import (
    CLOSED_TICKET_STATUSES,
    checklist_view,
    gather_facts,
    open_checklist,
    tickets_the_viewer_reads,
)
from app.modules.people.service import list_logins, load_person


async def _account(
    session: AsyncSession, org_id: UUID, person: Person, may_read_users: bool
) -> dict[str, Any] | None:
    if person.user_id is None:
        return None
    user = (
        await session.execute(
            select(User).where(User.org_id == org_id, User.id == person.user_id)
        )
    ).scalar_one_or_none()
    if user is None:
        return None
    roles = await user_role_names(session, user.id)
    return {
        "user_id": user.id,
        "kind": user.kind,
        "email": user.email,
        "status": user.status,
        "last_login_at": user.last_login_at,
        "console": any(role != "requester" for role in roles),
        "roles": roles if may_read_users else None,
    }


async def _devices(session: AsyncSession, org_id: UUID, person_id: UUID) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(Device, DevicePerson, DeviceGroup.name)
            .join(DevicePerson, DevicePerson.device_id == Device.id)
            .outerjoin(
                DeviceGroup,
                (DeviceGroup.id == Device.device_group_id) & (DeviceGroup.org_id == org_id),
            )
            .where(
                Device.org_id == org_id,
                DevicePerson.org_id == org_id,
                DevicePerson.person_id == person_id,
            )
            .order_by(Device.hostname)
        )
    ).all()
    return [
        {
            "device_id": device.id,
            "hostname": device.hostname,
            "os_family": device.os_family,
            "lifecycle": device.lifecycle,
            "device_group_id": device.device_group_id,
            "group_name": group_name,
            "last_seen_at": device.last_seen_at,
            "source": link.source,
            "observed_login": link.observed_login,
            "observed_at": link.observed_at,
        }
        for device, link, group_name in rows
    ]


async def _licences(
    session: AsyncSession, org_id: UUID, person_id: UUID, device_ids: list[UUID]
) -> list[dict[str, Any]]:
    held = (LicenseAllocation.person_id == person_id)
    if device_ids:
        held = held | (
            (LicenseAllocation.target_type == "device")
            & LicenseAllocation.target_id.in_(device_ids)
        )
    rows = (
        await session.execute(
            select(LicenseAllocation, SoftwareProduct.name)
            .join(License, License.id == LicenseAllocation.license_id)
            .join(SoftwareProduct, SoftwareProduct.id == License.product_id)
            .where(
                LicenseAllocation.org_id == org_id,
                License.org_id == org_id,
                LicenseAllocation.released_at.is_(None),
                held,
            )
            .order_by(func.lower(SoftwareProduct.name))
        )
    ).all()
    return [
        {
            "allocation_id": allocation.id,
            "license_id": allocation.license_id,
            "product_name": name,
            "through_device_id": (
                allocation.target_id if allocation.target_type == "device" else None
            ),
            "allocated_at": allocation.allocated_at,
        }
        for allocation, name in rows
    ]


async def _seats(session: AsyncSession, org_id: UUID, person_id: UUID) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(SeatAssignment, Subscription.service_name, Subscription.plan)
            .join(Subscription, Subscription.id == SeatAssignment.subscription_id)
            .where(
                SeatAssignment.org_id == org_id,
                Subscription.org_id == org_id,
                SeatAssignment.person_id == person_id,
                SeatAssignment.released_at.is_(None),
            )
            .order_by(func.lower(Subscription.service_name))
        )
    ).all()
    return [
        {
            "seat_id": seat.id,
            "subscription_id": seat.subscription_id,
            "service_name": service_name,
            "plan": plan,
            "assigned_at": seat.assigned_at,
        }
        for seat, service_name, plan in rows
    ]


async def _tickets(
    session: AsyncSession, org_id: UUID, user_id: UUID | None, viewer_id: UUID
) -> list[dict[str, Any]]:
    """The person's open tickets the viewer may read: each in a client where
    they hold `ticket.read`, which is not the person's client for a staff
    person (`tickets_the_viewer_reads`)."""
    if user_id is None:
        return []
    rows = (
        await session.execute(
            select(Ticket)
            .where(
                Ticket.org_id == org_id,
                Ticket.requester_id == user_id,
                Ticket.status.not_in(CLOSED_TICKET_STATUSES),
                tickets_the_viewer_reads(org_id, viewer_id),
            )
            .order_by(Ticket.created_at.desc())
        )
    ).scalars()
    return [
        {
            "ticket_id": t.id,
            "number": t.number,
            "subject": t.subject,
            "status": t.status,
            "priority": t.priority,
            "created_at": t.created_at,
        }
        for t in rows
    ]


async def person_detail(
    session: AsyncSession, *, org_id: UUID, viewer_id: UUID, person_id: UUID
) -> dict[str, Any]:
    person = await load_person(session, org_id, person_id)
    # What the viewer may read ABOUT THIS PERSON: the codes they hold on
    # every client, or on the person's own (plan 2026-10-26 Task 5). Without
    # the client, a technician of X saw X's people with every section hidden.
    held = await user_permission_codes(session, viewer_id, client_id=person.client_id)
    devices = await _devices(session, org_id, person.id)
    facts = await gather_facts(session, org_id, person, viewer_id=viewer_id)
    current = await open_checklist(session, org_id, person.id)
    history = (
        await session.execute(
            select(PersonChecklist)
            .where(
                PersonChecklist.org_id == org_id,
                PersonChecklist.person_id == person.id,
                PersonChecklist.status != "open",
            )
            .order_by(PersonChecklist.started_at.desc())
        )
    ).scalars()
    return {
        "person": person,
        "logins": await list_logins(session, org_id, person.id),
        "account": await _account(session, org_id, person, "users.read" in held),
        "devices": devices,
        "licences": (
            await _licences(session, org_id, person.id, [d["device_id"] for d in devices])
            if "licenses.read" in held
            else None
        ),
        "seats": await _seats(session, org_id, person.id) if "subscriptions.read" in held else None,
        "tickets": (
            await _tickets(session, org_id, person.user_id, viewer_id)
            if "ticket.read" in held
            else None
        ),
        "checklist": (
            await checklist_view(session, org_id, current, facts) if current is not None else None
        ),
        "past_checklists": [
            {"id": c.id, "kind": c.kind, "status": c.status, "closed_at": c.closed_at}
            for c in history
        ],
    }
